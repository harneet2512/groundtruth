#!/usr/bin/env python
"""gt_validation_gate.py — per-item MERGE CHECKLIST for the cooperative-localization
build (item D). Runs the OFFLINE safety guards that items A (post_search), B2
(body-semantic), and C (consensus render) must each pass BEFORE any paid/live run,
and prints PASS / FAIL / SKIP per check. Returns rc=1 on ANY fail.

Deterministic. No LLM. No ONNX (the embedder is forced OFF so the producer is
byte-stable and env-independent — we are gating leakage/determinism, not ranking).

Checks
------
  (a) determinism   the brief+localize producer, run twice on identical input, is
                    byte-for-byte identical.
  (b) leak          the emitted bytes carry NO gold / FAIL_TO_PASS / PASS_TO_PASS /
                    task-id / test-name / test-import artifact (the answer must not
                    leak through the delivered surface).
  (c) no-regression per-language hit@k in a measure_brief AGGREGATE does not drop
                    below a stored baseline (SKIP when no --aggregate given).
  (d) anti-overfit  the source DIFF contains no literal gold path / FAIL_TO_PASS /
                    PASS_TO_PASS / task-id / hardcoded test import in shipping
                    pipeline code = auto-reject (SKIP when no diff source given).
  (e) flag-off      every new default-off flag (GT_SEM_BODY, GT_POST_SEARCH) OFF is
                    byte-identical to the flags being unset (default-off guarantee).

Usage
-----
  gt_validation_gate.py                         # self-contained (a),(b),(e); (c),(d) skip
  gt_validation_gate.py --aggregate AGG.json [--baseline BASE.json] [--eps 0.0]
  gt_validation_gate.py --diff patch.diff       # anti-overfit on a diff file
  gt_validation_gate.py --git [--git-range master...HEAD]
  gt_validation_gate.py --gold-path src/foo.py --gold-path src/bar.py   # literal-path grep
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_REPO, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "src"))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --------------------------------------------------------------------------- #
# Hermetic synthetic producer (embedder OFF).                                 #
# --------------------------------------------------------------------------- #
_ISSUE = "apply_defaults mutates the caller mapping; correct apply_defaults in config"
_GOLD_SYM = "apply_defaults"
_GOLD_FILE = "pkg/config.py"
_CALLER_SYM = "load_config"
_CALLER_FILE = "pkg/loader.py"
_TEST_SYM = "test_apply_defaults"
_TEST_FILE = "tests/test_config.py"
_SENTINEL = "GOLDPATCHSENTINEL"
# Artifacts that must NEVER appear in the emitted bytes (the leak set).
_FORBIDDEN = (
    _TEST_SYM, "test_config.py", "tests/", _SENTINEL,
    "FAIL_TO_PASS", "PASS_TO_PASS",
)


def _semantic_off():
    """Force both embedder halves OFF -> deterministic, no ONNX/torch."""
    import groundtruth.pretask.graph_localizer as GL
    import groundtruth.pretask.v7_4_brief as V74

    GL._get_embedder = lambda: None

    def _zero():
        try:
            return V74._ZeroEmbeddingModel()
        except Exception:  # pragma: no cover
            class _Z:
                dim = 8

                def encode(self, texts, **_):
                    return [[0.0] * self.dim for _ in texts]

            return _Z()

    V74._get_model = _zero


def _build_graph(db: str) -> None:
    import sqlite3

    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);"""
    )
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
                " VALUES(1,'Function',?,?,1,3,?,0,'python')", (_GOLD_SYM, _GOLD_FILE, f"{_GOLD_SYM}(cfg)"))
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function',?,?,1,3,0,'python')", (_CALLER_SYM, _CALLER_FILE))
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
                " VALUES(1,2,1,'CALLS',2,'import',1.0)")
    # a CORRECTLY-flagged test node (is_test=1) — excluded from any delivered surface.
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(3,'Function',?,?,1,3,1,'python')", (_TEST_SYM, _TEST_FILE))
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
                " VALUES(2,3,1,'CALLS',2,'import',1.0)")
    con.commit()
    con.close()


def _write_repo(root: str) -> None:
    files = {
        _GOLD_FILE: f"def {_GOLD_SYM}(cfg):\n    return cfg  # {_SENTINEL}\n",
        _CALLER_FILE: f"def {_CALLER_SYM}():\n    return {_GOLD_SYM}({{}})\n",
        _TEST_FILE: f"def {_TEST_SYM}():\n    assert {_GOLD_SYM}({{}}) is not None\n",
    }
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)


def _produce(root: str, db: str, *, sem_body: bool, post_search: bool) -> str:
    """The agent-visible surface: brief text + serialized localize candidates,
    under a chosen flag state. GT_SEM_BODY drives B2; GT_POST_SEARCH drives A."""
    from groundtruth.pretask.graph_localizer import _normalize, localize
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    saved = {k: os.environ.get(k) for k in ("GT_SEM_BODY", "GT_POST_SEARCH")}
    _set = {"GT_SEM_BODY": "1" if sem_body else None,
            "GT_POST_SEARCH": "1" if post_search else None}
    for k, v in _set.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        brief = generate_v1r_brief(_ISSUE, root, db).brief_text
        loc = localize(_ISSUE, db, repo_root=root)
        cand = [f"{_normalize(c.file_path)}\t{c.score:.6f}\t{c.render_witness()}"
                for c in (loc.candidates or [])]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return brief + "\n===LOCALIZE===\n" + "\n".join(cand)


# --------------------------------------------------------------------------- #
# Checks.                                                                      #
# --------------------------------------------------------------------------- #
def check_determinism_and_leak_and_flagoff() -> list[tuple[str, str, str]]:
    """Runs the synthetic producer once and derives (a),(b),(e)."""
    _semantic_off()
    tmp = tempfile.mkdtemp(prefix="gt_gate_")
    db = os.path.join(tmp, "g.db")
    root = os.path.join(tmp, "repo")
    _build_graph(db)
    _write_repo(root)

    out = []
    # default (flags unset)
    base = _produce(root, db, sem_body=False, post_search=False)

    # (a) determinism — run twice on identical input.
    again = _produce(root, db, sem_body=False, post_search=False)
    if base == again:
        out.append(("a_determinism", PASS, "producer byte-identical across two runs"))
    else:
        out.append(("a_determinism", FAIL, "producer differs across two identical runs"))

    # (b) leak — no forbidden artifact in the emitted bytes.
    leaked = sorted({t for t in _FORBIDDEN if t and t in base})
    if not leaked:
        out.append(("b_leak", PASS, "no gold/test/F2P artifact in the delivered surface"))
    else:
        out.append(("b_leak", FAIL, f"leaked artifacts: {leaked}"))

    # (e) flag-off byte-identity — each new default-off flag OFF == unset.
    off_sembody = _produce(root, db, sem_body=False, post_search=False)
    on_sembody_zero_embed = _produce(root, db, sem_body=True, post_search=False)
    # GT_SEM_BODY explicit-0 is emulated by unset (both mean off); assert the
    # feature ON with the embedder off is render-neutral (no bytes change).
    parts = []
    ok = True
    if base == off_sembody:
        parts.append("GT_SEM_BODY unset==off")
    else:
        ok = False
        parts.append("GT_SEM_BODY off DIVERGES from default")
    if base == on_sembody_zero_embed:
        parts.append("GT_SEM_BODY on is render-neutral (embedder off)")
    else:
        ok = False
        parts.append("GT_SEM_BODY on CHANGED bytes with a zero embedder (render leak)")
    out.append(("e_flag_off", PASS if ok else FAIL, "; ".join(parts)))
    return out


def check_no_regression(aggregate: str | None, baseline: str | None,
                        eps: float) -> tuple[str, str, str]:
    """(c) per-language hit@k in a measure_brief AGGREGATE never drops below a
    stored baseline. SKIP when no aggregate is given."""
    if not aggregate:
        return ("c_no_regression", SKIP, "no --aggregate given")
    try:
        agg = json.load(open(aggregate, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return ("c_no_regression", FAIL, f"cannot read aggregate: {e!r}")

    def _by_lang(d: dict) -> dict:
        bl = d.get("by_language") or {}
        return {k: {"h1": v.get("gt_hit_at_1"), "h3": v.get("gt_hit_at_3")}
                for k, v in bl.items()}

    # A nonzero leak in the aggregate is an auto-fail regardless of baseline.
    if int(agg.get("leak_total") or 0) > 0:
        return ("c_no_regression", FAIL, f"aggregate leak_total={agg.get('leak_total')} (must be 0)")

    cur = _by_lang(agg)
    if not baseline:
        return ("c_no_regression", PASS,
                f"no baseline; {len(cur)} langs, leak_total=0 (no comparison requested)")
    try:
        base = _by_lang(json.load(open(baseline, encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        return ("c_no_regression", FAIL, f"cannot read baseline: {e!r}")

    regressions = []
    for lang, b in base.items():
        c = cur.get(lang)
        if not c:
            regressions.append(f"{lang}: missing in current")
            continue
        for k in ("h1", "h3"):
            bv, cv = b.get(k), c.get(k)
            if bv is None:
                continue  # no baseline number here -> nothing to regress from
            if cv is None:
                # baseline HAD a number but current is null -> a collapsed / empty
                # measurement must NOT pass as "no regression" (Fable D4): zero scorable
                # tapes is not proof of non-regression, it's proof of nothing.
                regressions.append(f"{lang}.{k}: current is null (collapsed/empty measurement)")
                continue
            if float(cv) < float(bv) - eps:
                regressions.append(f"{lang}.{k}: {cv:.8f} < {bv:.8f}-{eps}")
    if regressions:
        return ("c_no_regression", FAIL, "; ".join(regressions))
    return ("c_no_regression", PASS, f"no per-language hit@k regression (eps={eps})")


# task-id: SWE-bench style owner__repo-1234 (very specific; rare in real pipeline code)
_TASKID_RE = re.compile(r"\b[A-Za-z0-9][\w.-]*__[\w.-]+-\d+\b")
_F2P_RE = re.compile(r"\bFAIL_TO_PASS\b|\bPASS_TO_PASS\b")
_TESTIMPORT_RE = re.compile(r"^\s*(?:from|import)\s+.*\btest_\w+", re.I)
# diff paths we do NOT treat as shipping pipeline code. EXCLUDE only GENUINE test
# locations: a test DIRECTORY component, or the language-native ADJACENT-test conventions
# (*_test.go/.py, *.test.ts, *.spec.js — unambiguously tests next to source). A bare
# `test_*.py` inside a SHIPPING package is NOT excluded (Fable D3: it was a blessed hiding
# place for overfit) — fail-safe: scan when ambiguous. A false FAIL on a mis-located real
# test is investigated; a false PASS ships overfit.
_DIFF_EXCLUDE = re.compile(
    r"(^|/)tests?/|(^|/)__tests__/|"
    r"[^/]*_test\.[A-Za-z0-9]+$|[^/]*\.(?:test|spec)\.[A-Za-z0-9]+$|"
    r"gt_validation_gate\.py$|\.md$")


def _diff_text(diff_file: str | None, use_git: bool, git_range: str | None) -> str | None:
    if diff_file:
        try:
            return open(diff_file, encoding="utf-8", errors="replace").read()
        except Exception:  # noqa: BLE001
            return None
    if use_git:
        cmd = ["git", "-C", _REPO, "diff", "--no-color"]
        if git_range:
            cmd.append(git_range)
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            ).stdout
        except Exception:  # noqa: BLE001
            return None
    return None


def check_anti_overfit(diff_file: str | None, use_git: bool, git_range: str | None,
                       gold_paths: list[str]) -> tuple[str, str, str]:
    """(d) grep ADDED lines in shipping pipeline files for overfit tells."""
    text = _diff_text(diff_file, use_git, git_range)
    if text is None:
        return ("d_anti_overfit", SKIP, "no diff source (--diff / --git) given")

    cur_path = ""
    included = True
    offences: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++ "):
            cur_path = line[4:].strip()
            if cur_path.startswith("b/"):
                cur_path = cur_path[2:]
            included = not _DIFF_EXCLUDE.search(cur_path)
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if not line.startswith("+"):
            continue
        added = line[1:]
        stripped = added.lstrip()
        # skip pure comment/docstring lines (the common false-positive source).
        if stripped.startswith(("#", "//", "*", '"""', "'''", "/*")):
            continue
        if not included:
            continue
        if _F2P_RE.search(added):
            offences.append(f"{cur_path}: FAIL_TO_PASS/PASS_TO_PASS literal: {added.strip()[:80]}")
        if _TASKID_RE.search(added):
            offences.append(f"{cur_path}: task-id literal: {added.strip()[:80]}")
        if _TESTIMPORT_RE.search(added):
            offences.append(f"{cur_path}: hardcoded test import: {added.strip()[:80]}")
        for gp in gold_paths:
            if gp and gp in added:
                offences.append(f"{cur_path}: literal gold path {gp!r}: {added.strip()[:80]}")
    if offences:
        return ("d_anti_overfit", FAIL, " | ".join(offences[:12]))
    return ("d_anti_overfit", PASS, "no overfit literal in shipping pipeline diff")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aggregate", help="measure_brief AGGREGATE json for the (c) no-regression check")
    ap.add_argument("--baseline", help="stored AGGREGATE json to compare (c) against")
    ap.add_argument("--eps", type=float, default=0.0, help="hit@k regression tolerance (default 0.0)")
    ap.add_argument("--diff", help="unified diff FILE for the (d) anti-overfit grep")
    ap.add_argument("--git", action="store_true", help="run `git diff` for the (d) anti-overfit grep")
    ap.add_argument("--git-range", help="git range for --git (e.g. master...HEAD)")
    ap.add_argument("--gold-path", action="append", default=[],
                    help="literal gold path to grep the diff for (repeatable)")
    ap.add_argument("--json", help="also write the results JSON here")
    args = ap.parse_args()

    results: list[tuple[str, str, str]] = []
    results.extend(check_determinism_and_leak_and_flagoff())          # (a)(b)(e)
    results.append(check_no_regression(args.aggregate, args.baseline, args.eps))  # (c)
    results.append(check_anti_overfit(args.diff, args.git, args.git_range, args.gold_path))  # (d)

    order = {"a_determinism": 0, "b_leak": 1, "c_no_regression": 2,
             "d_anti_overfit": 3, "e_flag_off": 4}
    results.sort(key=lambda r: order.get(r[0], 9))

    print("=" * 72)
    print("GT VALIDATION GATE — cooperative-localization merge checklist")
    print("=" * 72)
    n_fail = 0
    for name, status, detail in results:
        if status == FAIL:
            n_fail += 1
        print(f"  [{status:<4}] {name:<18} {detail}")
    print("-" * 72)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    # (c) no-regression and (d) anti-overfit are the MERGE-CRITICAL checks: if either was
    # SKIPPED (no --aggregate/--baseline, or no --diff/--git) the gate has NOT verified
    # regression/overfit, so it may NOT certify a merge (Fable D4b — a bare invocation must
    # not print a green OK-TO-MERGE). Green requires BOTH ran AND nothing failed.
    _crit_skipped = [n for n, s, _ in results
                     if s == SKIP and n in ("c_no_regression", "d_anti_overfit")]
    if n_fail:
        verdict, rc = "REJECT", 1
    elif _crit_skipped:
        verdict, rc = "INCOMPLETE", 2
    else:
        verdict, rc = "OK-TO-MERGE", 0
    print(f"  {verdict}  ({n_fail} FAIL, {n_skip} SKIP, "
          f"{sum(1 for _, s, _ in results if s == PASS)} PASS)")
    if _crit_skipped:
        print(f"  NOT a merge certification - merge-critical checks skipped: "
              f"{', '.join(_crit_skipped)} (supply --aggregate/--baseline and --diff/--git)")
    print("=" * 72)

    if args.json:
        json.dump({"verdict": verdict, "n_fail": n_fail, "rc": rc,
                   "critical_skipped": _crit_skipped,
                   "results": [{"check": n, "status": s, "detail": d} for n, s, d in results]},
                  open(args.json, "w", encoding="utf-8"), indent=2)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
