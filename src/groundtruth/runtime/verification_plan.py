"""VerificationPlan — the progressive check ladder (B1 verification, planner half).

WHY THIS EXISTS. The B1 verification-execution stack SELECTS the repo's own
covering test FILES that reach an edited symbol (``covering_runner.select_covering_tests``)
and RUNS them (``covering_runner.run_covering_tests``). Measured against the test
files that genuinely reference the edited symbol, that DIRECT selector's recall is
~0.019: tests almost never call the exact edited function — they call a
higher-level entry point that transitively reaches it. This module raises recall
WITHOUT re-implementing the selector, and it replaces hard-coded ``["pytest", ...]``
commands with repo-config-discovered ones, so verification generalizes across
repos/languages.

WHAT IT IS. A PURE planner: ``build_verification_plan`` reads graph.db + repo
config and emits a ``VerificationPlan`` — an ordered ladder of ``Check`` rungs
(syntax -> unit(targeted) -> integration(whole-suite)), each labelled with the
selection basis that produced it and a confidence that DEGRADES with derivation
distance (direct FACT covering > k=2 closure > test-dir convention). It executes
NOTHING itself. ``run_plan(plan, executor)`` delegates each rung to an EXISTING
runner (``edit_check`` for syntax, ``covering_runner`` for unit,
``test_runner.execute_test_command`` for a discovered whole-suite command) through
the frozen Wave-1 executor contract ``(cmd, cwd, timeout) -> (exit, out, err)``.

THE LAWS (inherited, non-negotiable):
- Deterministic / LLM-free / no network / no task-IDs / no gold labels. Same
  fixture -> byte-identical (sorted) plan.
- Correct-or-quiet + ADVISORY: nothing here blocks. A missing/ambiguous toolchain
  yields a kind-level UNKNOWN rung (``command=None``, ``confidence="unknown"``),
  NEVER a guessed command. Every helper fails toward quiet, never raises.
- THE FACT RULE: a covering / closure edge counts only when its
  ``resolution_method`` is in ``curation_map.DETERMINISTIC_RESOLUTION_METHODS``
  (imported — the ONE canonical set) AND ``confidence >= 0.7``; ``name_match`` is
  never a fact; ``is_test`` nodes are excluded per the leak law.
- GREEN redefined honestly (``green``): executed AND fresh (graph+patch revision
  match the plan) AND covers >= 1 named entity/obligation AND attributable-when-
  required AND the underlying verdict is a pass. A bare exit-0 with no coverage
  mapping is ``executed_unmapped`` — never green.

CANONICAL-SELECTOR DISCIPLINE. All test selection routes THROUGH
``covering_runner.select_covering_tests`` / ``build_covering_command`` (the ONE
file-scoped, leak-safe surface). This module never re-declares the covering SQL
and never touches the mini's name-scoped twin.

COMMAND-DISCOVERY CAPABILITY TABLE (config source -> whole-suite command +
confidence; conservative parsing; absent/unparseable sources silently skipped.
Confidence is MEDIUM only for CORROBORATED shapes — a pytest ini section, or a
package.json test script naming a known runner token; every other discovery is
LOW, because a `test` LABEL alone does not prove the command runs tests — the
measured FP classes include a Makefile `test:` target that DEPLOYED):

    pyproject.toml  [tool.pytest.ini_options] (required — bare [tool.pytest] is
                    IGNORED by pytest and is NOT a trigger)
                                             -> ["pytest"]  config:pyproject.pytest  medium
    pytest.ini      (present)                -> ["pytest"]  config:pytest_ini        medium
    setup.cfg       [tool:pytest]            -> ["pytest"]  config:setup_cfg         medium
    package.json    scripts.test — the npm-init placeholder ("no test specified")
                    or a bare echo with no runner token is NEGATIVE evidence:
                    discovery returns UNKNOWN (config:package_json_placeholder)
                    and BLOCKS the fallback, since `npm test` would run that very
                    placeholder. Otherwise:
                                             -> ["npm","test"]  config:package_json
                                                medium iff the script names a known
                                                runner token (jest/mocha/vitest/...),
                                                else low
    Makefile        a column-0 `test:` TARGET line only — never a `test :=`/`test =`
                    variable assignment, never a tab-indented recipe line
                                             -> ["make","test"]      config:makefile  low
    go.mod          (present)                -> ["go","test","./..."] config:go_mod   low
    Cargo.toml      (present)                -> ["cargo","test"]      config:cargo    low
    [tool.poetry.scripts].test  NOT a trigger: that table declares console-script
                    ENTRY POINTS, not a task runner (`test = "pkg.deploy:main"` is a
                    measured false positive).
    (none of the above)  -> repo_adapters extension fallback   extension_fallback  low
    (fallback empty)     -> UNKNOWN (command=None, confidence="unknown")

PER-KIND CAPABILITY (what a rung can and cannot verify):

    syntax        edit_check: py/pyi/js/ts/tsx/jsx/go/rb parse-only checkers;
                  rs/java are UNAVAILABLE (edit_check honesty table). Verdict "ok" requires
                  EVERY checkable target to parse; a mix of ok + unavailable is
                  "partial" (never green).
    unit          targeted covering over FACT edges + k=2 caller closure + test-dir
                  convention. Confidence degrades: fact_covering(high) >
                  closure_k2(medium) > test_dir_convention(low, INFO).
                  ATTRIBUTION HONESTY: only a fact_covering-basis PASS is
                  self-attributed — the FACT edge (deterministic method + conf>=0.7)
                  structurally proves the selected test reaches the edited symbol. A
                  closure-basis pass proves only 2-hop transitive reach (the pass
                  does not prove the edited code executed on that test's path); a
                  convention-basis pass proves nothing structural. Both carry
                  attribution_satisfied=False -> green() says "attribution_unmet":
                  an ADVISORY pass, never green.
    integration   the repo's whole discovered test suite — or UNKNOWN. A PASS needs
                  POSITIVE evidence (>=1 test parsed from the runner's own output,
                  mirroring covering_runner._classify_one); a clean exit with ZERO
                  tests parsed is "executed_no_tests", NEVER green (the measured
                  attack: a discovered `make test` that deploys and exits 0).
    type|build|   NOT emitted unless a future obligation/config names a sound command
    property|e2e  (correct-or-quiet: no cheap, sound, language-agnostic discovery).

MEASURED RECALL HONESTY (gt_selection_pr --planner, 2026-07-10). The label-tier
recall win (micro-R 0.52 on the signature dataset vs 0.0 direct-covering) is
carried by the TEST-DIR CONVENTION lever: all 13 label-tier true positives were
selection_basis=test_dir_convention; closure_k2 contributed 0 predictions and 0
hits on that tier (grep tier: 33 convention TPs vs 10 closure TPs; closure
precision ~0.079). CAVEAT (F10): the label tier is convention-biased BY
CONSTRUCTION — a commit changing X.py predictably touches tests/test_X.py — so it
inflates exactly the convention lever. The closure lever must be judged on the
grep tier / future SHA-matched sets, where it is currently weak. No number here
may be cited as closure credit.

LLM-free, deterministic, no network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

try:  # Python 3.11+ stdlib; guarded so an exotic interpreter degrades to quiet.
    import tomllib  # type: ignore
except Exception:  # noqa: BLE001
    tomllib = None  # type: ignore

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.covering_runner import (
    _connect_ro,
    _edge_columns,
    attribute_covering_red,
    build_covering_command,
    run_covering_tests,
    select_covering_tests,
)
from groundtruth.runtime.edit_check import check_edit_syntax
from groundtruth.runtime.repo_adapters import is_test_file, select_repo_test_command
from groundtruth.runtime.test_runner import execute_test_command

# The injectable execution boundary — the SAME frozen contract Wave-1 established
# (test_runner / covering_runner / edit_check). ``None`` selects the host path.
Executor = Callable[[list[str], str, int], "tuple[int | None, str, str]"]

# FACT-tier resolution methods — the ONE canonical set (curation_map), never a
# re-declared literal. A covering / closure edge is a FACT only through one of these.
_DETERMINISTIC_METHODS = DETERMINISTIC_RESOLUTION_METHODS

# Selection caps (plan §3). k=2 closure over FACT-tier CALLS edges; <=50 intermediate
# caller nodes; <=8 total selected test files across ALL levers.
_CLOSURE_HOPS = 2
_CLOSURE_NODE_CAP = 50
_TOTAL_SELECTION_CAP = 8
# select_covering_tests takes <=20 target names per call (its internal LIMIT 20);
# closure names are batched at this width so the canonical selector is reused as-is.
_COVERING_NAME_BATCH = 20

# Verdict vocabularies (green law). A pass is POSITIVE success; a fail is POSITIVE
# failure; everything else is ambiguous (never green, never a block).
_PASS_VERDICTS = frozenset({"pass", "ok"})
_FAIL_VERDICTS = frozenset({"fail", "syntax_error"})

# Selection-basis labels (every selected file carries exactly one) and their
# confidence tier. Ordered strongest -> weakest; the precision guard.
_BASIS_FACT = "fact_covering"
_BASIS_CLOSURE = "closure_k2"
_BASIS_CONVENTION = "test_dir_convention"
_BASIS_CONFIDENCE = {
    _BASIS_FACT: "high",
    _BASIS_CLOSURE: "medium",
    _BASIS_CONVENTION: "low",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """One rung of the verification ladder. Frozen + tuple-valued so a plan is
    hashable and byte-identical across runs on the same fixture.

    ``command`` is ``None`` for a kind-level UNKNOWN (missing/ambiguous toolchain)
    and for the syntax rung (whose runner, ``edit_check``, needs no repo command).
    ``targets`` are the FILES the runner executes (syntax: the changed source
    files; unit: the selected covering test files). ``expected_cost`` /
    ``confidence`` are ORDINAL words (never fabricated numbers)."""

    kind: str  # syntax | type | build | unit | integration | property | e2e
    command: tuple[str, ...] | None
    selection_basis: str
    covered_entities: tuple[str, ...] = ()
    covered_obligations: tuple[str, ...] = ()
    expected_cost: str = "unknown"  # low | medium | high | unknown
    confidence: str = "unknown"  # high | medium | low | unknown
    attribution_requirement: str = "none"  # none | edit_attributed
    targets: tuple[str, ...] = ()
    reason: str = ""  # diagnostic (why UNKNOWN / which config source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "command": list(self.command) if self.command is not None else None,
            "selection_basis": self.selection_basis,
            "covered_entities": list(self.covered_entities),
            "covered_obligations": list(self.covered_obligations),
            "expected_cost": self.expected_cost,
            "confidence": self.confidence,
            "attribution_requirement": self.attribution_requirement,
            "targets": list(self.targets),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VerificationPlan:
    """The whole ladder for one patch revision. ``graph_revision`` +
    ``patch_revision`` are the FRESHNESS keys the green law checks against;
    ``edited_files`` are the source files of ``changed_entities`` (needed by
    run_plan for red-attribution). All fields are deterministic functions of the
    (graph_db, repo_root, changed_entities, obligations) inputs."""

    patch_revision: str
    graph_revision: str
    changed_entities: tuple[str, ...]
    obligations: tuple[str, ...]
    checks: tuple[Check, ...]
    edited_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_revision": self.patch_revision,
            "graph_revision": self.graph_revision,
            "changed_entities": list(self.changed_entities),
            "obligations": list(self.obligations),
            "edited_files": list(self.edited_files),
            "checks": [c.to_dict() for c in self.checks],
        }

    def canonical_json(self) -> str:
        """Byte-identical serialization for the determinism invariant."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CheckResult:
    """The outcome of executing one rung. Stamped with the revisions it ran under
    so ``green`` can prove freshness against the current plan."""

    kind: str
    selection_basis: str
    executed: bool
    # pass | fail | ok | syntax_error | partial | executed_no_tests | unavailable
    # | unknown | skipped  ("partial" = syntax rung with a mix of ok+unavailable
    # targets; "executed_no_tests" = clean exit with ZERO tests parsed — neither
    # is a pass verdict, neither can green)
    verdict: str
    graph_revision: str
    patch_revision: str
    covered_entities: tuple[str, ...]
    covered_obligations: tuple[str, ...]
    attribution_requirement: str
    attribution_satisfied: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GreenVerdict:
    green: bool
    status: str  # green | not_executed | red | unavailable | stale | attribution_unmet | executed_unmapped
    kind: str = ""


# ---------------------------------------------------------------------------
# Revision derivation (deterministic, content-derived — NEVER wall-clock/mtime)
# ---------------------------------------------------------------------------
def _sha12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def derive_graph_revision(graph_db: str) -> str:
    """A content signature of ``graph_db`` — stable for an unchanged graph, so the
    plan is byte-identical across runs, yet changes when the graph is reindexed.
    Never mtime (would break determinism). ``"absent"`` when no usable db."""
    if not graph_db or not os.path.isfile(graph_db):
        return "absent"
    con = _connect_ro(graph_db)
    if con is None:
        return "absent"
    try:
        n_nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        max_n = con.execute("SELECT COALESCE(MAX(id),0) FROM nodes").fetchone()[0]
        max_e = con.execute("SELECT COALESCE(MAX(id),0) FROM edges").fetchone()[0]
        return _sha12(f"{n_nodes}:{n_edges}:{max_n}:{max_e}")
    except sqlite3.Error:
        return "absent"
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def derive_patch_revision(changed_entities: Iterable[str]) -> str:
    """A deterministic identity of the changed-entity set. The caller may override
    with a real diff/content hash; absent that, the sorted entity set IS the
    identity (a different patch touching a different entity set -> different rev)."""
    ents = sorted({str(e) for e in (changed_entities or ()) if e})
    if not ents:
        return "empty"
    return _sha12("\n".join(ents))


# ---------------------------------------------------------------------------
# Command discovery (repo config BEFORE the extension fallback)
# ---------------------------------------------------------------------------
# Known JS/TS test-runner tokens: a package.json scripts.test naming one of these
# is CORROBORATED (confidence medium); anything else is an uncorroborated label
# (confidence low). Word-boundary matched, deterministic.
_JS_RUNNER_TOKEN_RE = re.compile(
    r"\b(jest|mocha|vitest|ava|tap|tape|jasmine|karma|cypress|playwright|"
    r"uvu|qunit|nyc|c8|node|deno|bun)\b"
)
# A column-0 Makefile TARGET line `test:` (optionally `test :`, double-colon ok) —
# NOT a variable assignment (`test :=` / `test =` / `test ?=` / `test +=`) and,
# because the match is anchored at column 0, never a tab-indented recipe line.
_MAKE_TEST_TARGET_RE = re.compile(r"^test[ \t]*:(?!=)")


def discover_test_command(repo_root: str) -> tuple[tuple[str, ...] | None, str, str]:
    """The repo's own whole-suite test command from CONFIG, or the extension
    fallback, or UNKNOWN. Returns ``(command_tuple | None, basis, confidence)``.

    Conservative: each config source is parsed defensively; a source that is
    absent, unreadable, or unparseable is silently skipped (never a guess). When
    nothing config-level matches, defer to ``repo_adapters.select_repo_test_command``
    (manifest/extension fallback, confidence low); when THAT is empty, return
    ``(None, "unknown", "unknown")`` so the caller emits a kind-level UNKNOWN rung —
    never a fabricated command.

    CONFIDENCE LAW (F3): "medium" ONLY for corroborated shapes — a pytest ini
    section (pyproject ini_options / pytest.ini / setup.cfg [tool:pytest]) or a
    package.json test script that names a known runner token. Every other
    discovery — Makefile target, go.mod, Cargo.toml, extension fallback, or a
    package.json script with no runner token — is "low": a `test` LABEL does not
    prove the command runs tests (measured FP: a `test:` target that deploys)."""
    if not repo_root or not os.path.isdir(repo_root):
        return None, "unknown", "unknown"
    for probe in (
        _cfg_pyproject,
        _cfg_pytest_ini,
        _cfg_setup_cfg,
        _cfg_package_json,
        _cfg_makefile,
        _cfg_go_mod,
        _cfg_cargo,
    ):
        try:
            found = probe(repo_root)
        except Exception:  # noqa: BLE001 -- correct-or-quiet: a bad config is "absent"
            found = None
        if found is not None:
            return found
    # Config silent -> manifest/extension fallback (repo_adapters, ONE surface).
    try:
        cmd, _reason = select_repo_test_command(repo_root)
    except Exception:  # noqa: BLE001
        cmd = []
    if cmd:
        return tuple(cmd), "extension_fallback", "low"
    return None, "unknown", "unknown"


def _read_text(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _cfg_pyproject(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    """[tool.pytest.ini_options] ONLY. A bare [tool.pytest] table is IGNORED by
    pytest itself (it reads ini_options exclusively) — treating it as a trigger was
    a measured FP (F3c2). [tool.poetry.scripts].test is NOT a trigger: that table
    declares console-script ENTRY POINTS (name -> "module:function"), not a task
    runner; `test = "pkg.deploy:main"` was a measured FP (F3c)."""
    raw = _read_text(os.path.join(repo_root, "pyproject.toml"))
    if raw is None or tomllib is None:
        return None
    try:
        data = tomllib.loads(raw)
    except Exception:  # noqa: BLE001 -- malformed toml -> absent
        return None
    tool = data.get("tool") if isinstance(data, dict) else None
    if not isinstance(tool, dict):
        return None
    pytest_cfg = tool.get("pytest")
    if isinstance(pytest_cfg, dict) and isinstance(pytest_cfg.get("ini_options"), dict):
        return ("pytest",), "config:pyproject.pytest", "medium"
    return None


def _cfg_pytest_ini(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    if os.path.isfile(os.path.join(repo_root, "pytest.ini")):
        return ("pytest",), "config:pytest_ini", "medium"
    return None


def _cfg_setup_cfg(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    raw = _read_text(os.path.join(repo_root, "setup.cfg"))
    if raw is None:
        return None
    # [tool:pytest] section is the canonical pytest config in setup.cfg.
    if "[tool:pytest]" in raw:
        return ("pytest",), "config:setup_cfg", "medium"
    return None


def _cfg_package_json(repo_root: str) -> tuple[tuple[str, ...] | None, str, str] | None:
    """scripts.test, with the F3b placeholder guard: the npm-init default
    (`echo "Error: no test specified" && exit 1`) and a bare echo that names no
    runner token are NOT test commands. A placeholder is returned as NEGATIVE
    evidence ``(None, "config:package_json_placeholder", "unknown")`` — not a
    silent skip — because the extension fallback would otherwise re-emit
    ``npm test``, which runs THAT EXACT placeholder script (the measured FP would
    survive the skip). Positive placeholder evidence therefore terminates
    discovery at UNKNOWN. A script naming a known runner token is corroborated
    (medium); any other non-placeholder script is an uncorroborated label (low)."""
    raw = _read_text(os.path.join(repo_root, "package.json"))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 -- malformed json -> absent
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not (isinstance(scripts, dict) and isinstance(scripts.get("test"), str)):
        return None
    script = scripts["test"].strip()
    if not script:
        return None
    low = script.lower()
    has_runner = bool(_JS_RUNNER_TOKEN_RE.search(low))
    # npm-init placeholder / bare echo with no runner -> POSITIVE evidence the repo
    # declares NO test command; block the fallback (it would run this very script).
    if "no test specified" in low or (low.startswith("echo") and not has_runner):
        return None, "config:package_json_placeholder", "unknown"
    # Conservative: run the declared script via the package manager's `test`
    # lifecycle (npm test) rather than re-tokenizing an arbitrary shell string.
    return ("npm", "test"), "config:package_json", ("medium" if has_runner else "low")


def _cfg_makefile(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    """A column-0 `test:` TARGET line only (``_MAKE_TEST_TARGET_RE``). Excluded by
    construction (measured FPs, F3a2): variable assignments (`test := ./deploy.sh`
    — the old ``A or (B and C)`` precedence admitted these) and tab-indented recipe
    lines (never column 0). Confidence is LOW always: a target NAMED test can run
    anything (measured FP: it deployed)."""
    raw = _read_text(os.path.join(repo_root, "Makefile"))
    if raw is None:
        return None
    for line in raw.splitlines():
        # Match against the RAW line: targets start at column 0; recipe lines are
        # tab-indented and must not match.
        if _MAKE_TEST_TARGET_RE.match(line):
            return ("make", "test"), "config:makefile", "low"
    return None


def _cfg_go_mod(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    if os.path.isfile(os.path.join(repo_root, "go.mod")):
        return ("go", "test", "./..."), "config:go_mod", "low"
    return None


def _cfg_cargo(repo_root: str) -> tuple[tuple[str, ...], str, str] | None:
    if os.path.isfile(os.path.join(repo_root, "Cargo.toml")):
        return ("cargo", "test"), "config:cargo", "low"
    return None


# ---------------------------------------------------------------------------
# Graph reads (entity -> files/ids) + the k=2 FACT-tier caller closure
# ---------------------------------------------------------------------------
def _entity_files(con: sqlite3.Connection, entities: Sequence[str]) -> dict[str, list[str]]:
    """name -> sorted non-test defining files (Function/Method/Class). For syntax
    targeting + convention module stems. Correct-or-quiet on any error."""
    ents = sorted({str(e) for e in entities if e})
    if not ents:
        return {}
    ph = ",".join("?" * len(ents))
    out: dict[str, set[str]] = {}
    try:
        rows = con.execute(
            f"SELECT DISTINCT name, file_path FROM nodes "
            f"WHERE name IN ({ph}) AND COALESCE(is_test,0)=0 "
            f"AND label IN ('Function','Method','Class')",
            ents,
        ).fetchall()
    except sqlite3.Error:
        return {}
    for name, fpath in rows:
        if name and fpath:
            out.setdefault(name, set()).add(fpath.replace("\\", "/").lstrip("./"))
    return {k: sorted(v) for k, v in out.items()}


def _seed_node_ids(con: sqlite3.Connection, entities: Sequence[str]) -> list[int]:
    ents = sorted({str(e) for e in entities if e})
    if not ents:
        return []
    ph = ",".join("?" * len(ents))
    try:
        rows = con.execute(
            f"SELECT id FROM nodes WHERE name IN ({ph}) AND COALESCE(is_test,0)=0",
            ents,
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted({int(r[0]) for r in rows if r and r[0] is not None})


def _fact_caller_closure(
    con: sqlite3.Connection, seed_ids: list[int], *, has_conf: bool
) -> list[int]:
    """Bounded k=2 transitive-CALLER closure over FACT-tier CALLS edges.

    A test that directly calls a CALLER of the edited symbol transitively exercises
    the edit — so expanding the target set UP the call graph (incoming CALLS edges)
    is the recall lever. Verified-only (``_DETERMINISTIC_METHODS`` + conf>=0.7),
    non-test intermediates, deterministic BFS ordered by node id, <=``_CLOSURE_HOPS``
    hops, <=``_CLOSURE_NODE_CAP`` intermediate nodes. Returns the intermediate
    caller node ids (never the seeds). Correct-or-quiet on any error."""
    if not seed_ids:
        return []
    det = "','".join(sorted(_DETERMINISTIC_METHODS))
    conf_gate = "AND COALESCE(e.confidence,0) >= 0.7 " if has_conf else ""
    frontier = list(seed_ids)
    visited = set(seed_ids)
    collected: list[int] = []
    for _hop in range(_CLOSURE_HOPS):
        if not frontier or len(collected) >= _CLOSURE_NODE_CAP:
            break
        fph = ",".join("?" * len(frontier))
        try:
            rows = con.execute(
                f"SELECT DISTINCT e.source_id FROM edges e "
                f"JOIN nodes n ON n.id = e.source_id "
                f"WHERE e.target_id IN ({fph}) AND e.type='CALLS' "
                f"AND COALESCE(n.is_test,0)=0 "
                f"AND LOWER(TRIM(e.resolution_method)) IN ('{det}') {conf_gate}"
                f"ORDER BY e.source_id",
                frontier,
            ).fetchall()
        except sqlite3.Error:
            break
        next_frontier: list[int] = []
        for (sid,) in rows:
            if sid is None:
                continue
            sid = int(sid)
            if sid in visited:
                continue
            if len(collected) >= _CLOSURE_NODE_CAP:
                break
            visited.add(sid)
            collected.append(sid)
            next_frontier.append(sid)
        frontier = next_frontier
    return collected


def _names_for_ids(con: sqlite3.Connection, ids: list[int]) -> list[str]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    try:
        rows = con.execute(
            f"SELECT DISTINCT name FROM nodes WHERE id IN ({ph}) "
            f"AND name IS NOT NULL AND COALESCE(is_test,0)=0",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted({r[0] for r in rows if r and r[0]})


def _covering_for_names(graph_db: str, names: Sequence[str]) -> list[dict[str, Any]]:
    """Union of ``select_covering_tests`` over ``names`` (batched to the canonical
    selector's <=20-target window). Deterministic; keeps the max confidence per
    file. Routes 100% through the canonical selector — no covering SQL here."""
    names = sorted({str(n) for n in names if n})
    if not names or not graph_db:
        return []
    best: dict[str, float] = {}
    for i in range(0, len(names), _COVERING_NAME_BATCH):
        batch = set(names[i : i + _COVERING_NAME_BATCH])
        for row in select_covering_tests(graph_db, batch, limit=_TOTAL_SELECTION_CAP):
            f = str(row.get("file") or "").replace("\\", "/").lstrip("./")
            if not f:
                continue
            c = float(row.get("confidence") or 0.0)
            if f not in best or c > best[f]:
                best[f] = c
    return [{"file": f, "confidence": best[f]} for f in sorted(best, key=lambda k: (-best[k], k))]


# ---------------------------------------------------------------------------
# test-directory CONVENTION candidates (INFO basis — filesystem heuristic)
# ---------------------------------------------------------------------------
def _module_stems(entity_files: dict[str, list[str]]) -> set[str]:
    """Module basenames (no ext) of the changed entities' files, len>=3."""
    stems: set[str] = set()
    for files in entity_files.values():
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            if stem and len(stem) >= 3 and stem != "__init__":
                stems.add(stem.lower())
    return stems


def _convention_candidates(repo_root: str, stems: set[str]) -> list[str]:
    """Repo test files whose CORE stem (test/spec affixes stripped) equals a
    changed module stem — the classical RTS file heuristic (Ekstazi ISSTA'15).
    Bounded single walk, vendored dirs skipped. INFO-quality; deterministic."""
    if not repo_root or not stems or not os.path.isdir(repo_root):
        return []
    hits: set[str] = set()
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target"}
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), repo_root).replace("\\", "/").lstrip("./")
            if not is_test_file(rel):
                continue
            core = _core_test_stem(fn)
            if core and core in stems:
                hits.add(rel)
    return sorted(hits)


def _core_test_stem(filename: str) -> str:
    """Strip test/spec affixes from a test filename to its subject stem, lowered.
    ``test_dates.py`` -> ``dates``; ``dates_test.go`` -> ``dates``;
    ``dates.test.ts`` -> ``dates``; ``dates.spec.js`` -> ``dates``."""
    base = os.path.basename(filename)
    # drop compound test/spec extensions first: foo.test.ts / foo.spec.tsx
    low = base.lower()
    for mid in (".test.", ".spec."):
        if mid in low:
            return low.split(mid, 1)[0]
    stem = os.path.splitext(base)[0].lower()
    if stem.startswith("test_"):
        return stem[5:]
    if stem.startswith("test-"):
        return stem[5:]
    if stem.endswith("_test"):
        return stem[:-5]
    if stem.endswith("_spec"):
        return stem[:-5]
    return stem


# ---------------------------------------------------------------------------
# Targeted selection (the recall levers, unioned + capped) — the eval entry point
# ---------------------------------------------------------------------------
def select_targeted_tests(
    graph_db: str,
    repo_root: str,
    changed_entities: Sequence[str],
    *,
    limit: int = _TOTAL_SELECTION_CAP,
) -> list[dict[str, Any]]:
    """The k=2-expanded covering selection: direct FACT covering + k=2 caller
    closure + test-dir convention, unioned, deterministic, capped at ``limit``
    (<=8) with precision-ordered priority (fact > closure > convention).

    Returns ``[{"file","confidence","selection_basis"}]``. Each file carries the
    STRONGEST basis that selected it. Pure read; correct-or-quiet ([] on bad db)."""
    ents = sorted({str(e) for e in (changed_entities or ()) if e})
    if not ents or not graph_db or not os.path.isfile(graph_db):
        return []
    con = _connect_ro(graph_db)
    if con is None:
        return []
    try:
        has_conf = "confidence" in _edge_columns(con)
        entity_files = _entity_files(con, ents)
        seed_ids = _seed_node_ids(con, ents)
        closure_ids = _fact_caller_closure(con, seed_ids, has_conf=has_conf)
        closure_names = _names_for_ids(con, closure_ids)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass

    # Lever 1 — DIRECT fact covering (highest confidence).
    direct = _covering_for_names(graph_db, ents)
    # Lever 2 — k=2 caller closure, covering on the expanded name set.
    closure = _covering_for_names(graph_db, closure_names) if closure_names else []
    # Lever 3 — test-dir convention candidates (INFO).
    convention = _convention_candidates(repo_root, _module_stems(entity_files))

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(files_conf: list[tuple[str, float]], basis: str) -> None:
        for f, c in files_conf:
            if len(selected) >= limit:
                return
            if f in seen:
                continue
            seen.add(f)
            selected.append({"file": f, "confidence": round(float(c), 8), "selection_basis": basis})

    _add([(r["file"], r["confidence"]) for r in direct], _BASIS_FACT)
    _add([(r["file"], r["confidence"]) for r in closure], _BASIS_CLOSURE)
    # convention carries no edge confidence; use a nominal low weight for ordering.
    _add([(f, 0.0) for f in convention], _BASIS_CONVENTION)
    return selected


# ---------------------------------------------------------------------------
# The pure planner
# ---------------------------------------------------------------------------
def build_verification_plan(
    graph_db: str,
    repo_root: str,
    changed_entities: Sequence[str],
    obligations: Sequence[str] = (),
    *,
    patch_revision: str | None = None,
    graph_revision: str | None = None,
) -> VerificationPlan:
    """Build the progressive check ladder for ``changed_entities``. PURE — executes
    nothing. Deterministic: the same (graph_db, repo_root, entities, obligations)
    yields a byte-identical plan (``canonical_json``).

    Ladder emitted (correct-or-quiet — a rung it cannot populate is UNKNOWN):
      1. syntax     — one rung over the changed source files (edit_check capability).
      2. unit       — one rung PER selection basis with a non-empty target set
                      (fact_covering / closure_k2 / test_dir_convention), confidence
                      degrading with derivation distance.
      3. integration — the repo's whole discovered test suite (or UNKNOWN)."""
    ents = tuple(sorted({str(e) for e in (changed_entities or ()) if e}))
    obl = tuple(sorted({str(o) for o in (obligations or ()) if o}))
    grev = graph_revision if graph_revision is not None else derive_graph_revision(graph_db)
    prev = patch_revision if patch_revision is not None else derive_patch_revision(ents)

    # Resolve entities -> changed source files (for syntax + attribution).
    edited_files: tuple[str, ...] = ()
    if graph_db and os.path.isfile(graph_db):
        con = _connect_ro(graph_db)
        if con is not None:
            try:
                ef = _entity_files(con, ents)
            finally:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            files: set[str] = set()
            for fl in ef.values():
                files.update(fl)
            edited_files = tuple(sorted(files))

    checks: list[Check] = []
    checks.append(_syntax_rung(ents, obl, edited_files))
    checks.extend(_unit_rungs(graph_db, repo_root, ents, obl))
    checks.append(_integration_rung(repo_root, ents, obl))

    return VerificationPlan(
        patch_revision=prev,
        graph_revision=grev,
        changed_entities=ents,
        obligations=obl,
        checks=tuple(checks),
        edited_files=edited_files,
    )


# edit_check languages that yield POSITIVE syntax evidence (its honesty table).
_SYNTAX_CHECKABLE_EXTS = frozenset(
    {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rb"}
)


def _syntax_rung(
    ents: tuple[str, ...], obl: tuple[str, ...], edited_files: tuple[str, ...]
) -> Check:
    """The syntax rung. Confidence reflects edit_check capability over the changed
    files' languages; UNKNOWN when no file is resolvable/checkable."""
    checkable = [
        f for f in edited_files if os.path.splitext(f)[1].lower() in _SYNTAX_CHECKABLE_EXTS
    ]
    if not edited_files:
        return Check(
            kind="syntax",
            command=None,
            selection_basis="edit_check",
            covered_entities=ents,
            covered_obligations=obl,
            expected_cost="low",
            confidence="unknown",
            attribution_requirement="none",
            targets=(),
            reason="no changed source files resolved from graph",
        )
    if not checkable:
        return Check(
            kind="syntax",
            command=None,
            selection_basis="edit_check",
            covered_entities=ents,
            covered_obligations=obl,
            expected_cost="low",
            confidence="unknown",
            attribution_requirement="none",
            targets=tuple(edited_files),
            reason="no syntax-checkable language among changed files",
        )
    conf = "high" if len(checkable) == len(edited_files) else "medium"
    return Check(
        kind="syntax",
        command=None,
        selection_basis="edit_check",
        covered_entities=ents,
        covered_obligations=obl,
        expected_cost="low",
        confidence=conf,
        attribution_requirement="none",
        targets=tuple(sorted(checkable)),
        reason="edit_check parse-only over changed source files",
    )


def _unit_rungs(
    graph_db: str, repo_root: str, ents: tuple[str, ...], obl: tuple[str, ...]
) -> list[Check]:
    """One unit rung per selection basis that produced targets (fact/closure/
    convention), targets partitioned so total across rungs is <=8."""
    selected = select_targeted_tests(graph_db, repo_root, ents)
    by_basis: dict[str, list[str]] = {}
    for row in selected:
        by_basis.setdefault(row["selection_basis"], []).append(row["file"])
    rungs: list[Check] = []
    for basis in (_BASIS_FACT, _BASIS_CLOSURE, _BASIS_CONVENTION):
        files = sorted(by_basis.get(basis, []))
        if not files:
            continue
        rungs.append(
            Check(
                kind="unit",
                command=tuple(build_covering_command(files[0]) or ()) or None,
                selection_basis=basis,
                covered_entities=ents,
                covered_obligations=obl,
                expected_cost="medium",
                confidence=_BASIS_CONFIDENCE[basis],
                attribution_requirement="edit_attributed",
                targets=tuple(files),
                reason=f"{len(files)} covering file(s) via {basis}",
            )
        )
    return rungs


def _integration_rung(repo_root: str, ents: tuple[str, ...], obl: tuple[str, ...]) -> Check:
    """The whole-suite rung from discovered config (or UNKNOWN). Confidence is the
    DISCOVERY's confidence (F3): medium only for corroborated shapes, else low —
    never a blanket medium for anything a probe emitted."""
    cmd, basis, discovered_conf = discover_test_command(repo_root)
    if cmd is None:
        return Check(
            kind="integration",
            command=None,
            selection_basis=basis,
            covered_entities=ents,
            covered_obligations=obl,
            expected_cost="high",
            confidence="unknown",
            attribution_requirement="edit_attributed",
            targets=(),
            reason="no test command discoverable from repo config or manifests",
        )
    return Check(
        kind="integration",
        command=tuple(cmd),
        selection_basis=basis,
        covered_entities=ents,
        covered_obligations=obl,
        expected_cost="high",
        confidence=discovered_conf,
        attribution_requirement="edit_attributed",
        targets=(),
        reason=f"whole-suite command discovered via {basis}",
    )


# ---------------------------------------------------------------------------
# Execution (delegates to the frozen runners; never re-implements a runner)
# ---------------------------------------------------------------------------
def run_plan(
    plan: VerificationPlan,
    executor: Executor | None = None,
    *,
    syntax_executor: Executor | None = None,
    repo_root: str = "",
    per_file_timeout: int = 120,
    total_budget_seconds: int = 300,
) -> list[CheckResult]:
    """Execute each rung through its existing runner and return stamped results.

    Delegation: syntax -> ``edit_check.check_edit_syntax`` per file; unit ->
    ``covering_runner.run_covering_tests`` over the rung's targets; integration ->
    ``test_runner.execute_test_command`` over the discovered command. A rung whose
    toolchain/command is UNKNOWN, or that raises, becomes an ``unavailable`` result
    (correct-or-quiet — never crashes the run, never blocks)."""
    results: list[CheckResult] = []
    started = time.monotonic()
    for check in plan.checks:
        elapsed = max(0.0, time.monotonic() - started)
        remaining_budget = max(
            0,
            int(total_budget_seconds - elapsed),
        )
        if remaining_budget <= 0:
            results.append(
                CheckResult(
                    kind=check.kind,
                    selection_basis=check.selection_basis,
                    executed=False,
                    verdict="unavailable",
                    graph_revision=plan.graph_revision,
                    patch_revision=plan.patch_revision,
                    covered_entities=check.covered_entities,
                    covered_obligations=check.covered_obligations,
                    attribution_requirement=check.attribution_requirement,
                    attribution_satisfied=False,
                    detail={"reason": "total_budget_exhausted"},
                )
            )
            continue
        try:
            res = _run_one(
                check,
                plan,
                executor,
                executor if syntax_executor is None else syntax_executor,
                repo_root,
                min(per_file_timeout, remaining_budget),
                remaining_budget,
            )
        except Exception as exc:  # noqa: BLE001 -- a rung must never brick the loop
            res = CheckResult(
                kind=check.kind,
                selection_basis=check.selection_basis,
                executed=False,
                verdict="unavailable",
                graph_revision=plan.graph_revision,
                patch_revision=plan.patch_revision,
                covered_entities=check.covered_entities,
                covered_obligations=check.covered_obligations,
                attribution_requirement=check.attribution_requirement,
                attribution_satisfied=False,
                detail={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
        results.append(res)
    return results


def _run_one(
    check: Check,
    plan: VerificationPlan,
    executor: Executor | None,
    syntax_executor: Executor | None,
    repo_root: str,
    per_file_timeout: int,
    total_budget_seconds: int,
) -> CheckResult:
    def _mk(executed: bool, verdict: str, attributed: bool, detail: dict[str, Any]) -> CheckResult:
        return CheckResult(
            kind=check.kind,
            selection_basis=check.selection_basis,
            executed=executed,
            verdict=verdict,
            graph_revision=plan.graph_revision,
            patch_revision=plan.patch_revision,
            covered_entities=check.covered_entities,
            covered_obligations=check.covered_obligations,
            attribution_requirement=check.attribution_requirement,
            attribution_satisfied=attributed,
            detail=detail,
        )

    if check.kind == "syntax":
        if not check.targets:
            return _mk(False, "unavailable", True, {"reason": check.reason})
        per: list[dict[str, Any]] = []
        n_err = n_ok = 0
        syntax_started = time.monotonic()
        for index, f in enumerate(check.targets):
            remaining = total_budget_seconds - (time.monotonic() - syntax_started)
            if remaining < 1:
                per.extend(
                    {
                        "file": pending,
                        "verdict": "unavailable",
                        "reason": "total_budget_exhausted",
                    }
                    for pending in check.targets[index:]
                )
                break
            r = check_edit_syntax(
                f,
                repo_root,
                executor=syntax_executor,
                timeout=max(1, min(10, int(remaining))),
            )
            per.append(r)
            if r.get("verdict") == "syntax_error":
                n_err += 1
            elif r.get("verdict") == "ok":
                n_ok += 1
        # F9: "ok" requires EVERY checkable target to parse. Any error -> the rung
        # is a syntax_error. A mix of ok + unavailable -> "partial" (executed, but
        # NOT a pass verdict -> never green); all unavailable -> "unavailable".
        if n_err > 0:
            verdict = "syntax_error"
        elif n_ok == len(check.targets):
            verdict = "ok"
        elif n_ok > 0:
            verdict = "partial"
        else:
            verdict = "unavailable"
        executed = (n_err + n_ok) > 0
        # A source file's own parse is inherently self-attributed (no test identity).
        return _mk(executed, verdict, True, {"per_file": per})

    if check.kind == "unit":
        if not check.targets:
            return _mk(False, "unavailable", False, {"reason": "no targets"})
        res = run_covering_tests(
            repo_root,
            list(check.targets),
            executor=executor,
            per_file_timeout=per_file_timeout,
            total_budget_seconds=total_budget_seconds,
        )
        verdict = res.get("verdict", "unavailable")
        if verdict == "fail":
            attribution = attribute_covering_red(
                res,
                set(plan.edited_files),
                test_files=list(check.targets),
                repo_root=repo_root,
                covering_files=list(check.targets),
                executor=executor,
            )
            attributed = attribution.attributed
            # Add producer-owned causal evidence to host-side plan telemetry.
            # This never enters a renderer or changes model-observation bytes.
            res = dict(res)
            res["attribution"] = attribution.to_dict()
        elif verdict == "pass":
            # F5 ATTRIBUTION HONESTY: only a fact_covering-basis pass is
            # self-attributed — the FACT edge (deterministic resolution + conf>=0.7)
            # structurally PROVES the selected test reaches the edited symbol. A
            # closure_k2 pass proves only 2-hop transitive reach (the pass does not
            # prove the edited code executed on that test's path); a
            # test_dir_convention pass proves nothing structural (filename match).
            # Both stay attribution_satisfied=False -> green() = "attribution_unmet"
            # -> an ADVISORY pass, never green.
            attributed = check.selection_basis == _BASIS_FACT
        else:
            attributed = False
        return _mk(bool(res.get("executed")), verdict, attributed, res)

    if check.kind == "integration":
        if check.command is None:
            return _mk(False, "unknown", False, {"reason": check.reason})
        res = execute_test_command(
            repo_root,
            list(check.command),
            timeout_seconds=max(1, min(120, total_budget_seconds)),
            executor=executor,
        )
        n_passed = int(res.get("passed") or 0)
        n_failed = int(res.get("failed") or 0)
        n_errored = int(res.get("errored") or 0)
        if not res.get("executed"):
            verdict, attributed = "unavailable", False
        elif res.get("all_passed") and n_passed > 0:
            # F2: a PASS needs POSITIVE evidence — >=1 test parsed from the runner's
            # own output (covering_runner._classify_one:202-203 discipline). A
            # whole-suite pass with parsed tests genuinely covers the edit.
            verdict, attributed = "pass", True
        elif res.get("all_passed"):
            # F2: exit-0 with ZERO tests parsed is NOT verification — the measured
            # attack was a discovered `make test` that DEPLOYED and exited 0.
            # "executed_no_tests" is not in _PASS_VERDICTS -> never green.
            verdict, attributed = "executed_no_tests", False
        elif n_failed > 0 or n_errored > 0:
            verdict, attributed = "fail", False  # whole-suite fail is not edit-attributed
        else:
            verdict, attributed = "unavailable", False
        return _mk(bool(res.get("executed")), verdict, attributed, res)

    # Unhandled kind (type/build/property/e2e via a future command) -> generic run.
    if check.command is None:
        return _mk(False, "unknown", False, {"reason": check.reason or "no command"})
    res = execute_test_command(
        repo_root,
        list(check.command),
        timeout_seconds=max(1, min(120, total_budget_seconds)),
        executor=executor,
    )
    # Same positive-evidence law as integration: exit-0 + zero parsed tests is
    # "executed_no_tests", never a pass.
    if res.get("all_passed") and int(res.get("passed") or 0) > 0:
        verdict = "pass"
    elif res.get("all_passed"):
        verdict = "executed_no_tests"
    elif not res.get("executed"):
        verdict = "unavailable"
    else:
        verdict = "fail"
    return _mk(bool(res.get("executed")), verdict, verdict == "pass", res)


# ---------------------------------------------------------------------------
# GREEN law — the honest verdict
# ---------------------------------------------------------------------------
def green(result: CheckResult, plan: VerificationPlan) -> GreenVerdict:
    """A check is GREEN only if it EXECUTED, is FRESH (graph+patch revision match
    the plan), COVERS >= 1 entity/obligation THE PLAN NAMES, is ATTRIBUTABLE-when-
    required, and its underlying verdict is a PASS. A bare exit-0 with no coverage
    mapping — OR a coverage claim over entities the plan never named (F4: phantom
    entities) — is ``executed_unmapped``, never green. ``executed_no_tests`` /
    ``partial`` are not pass verdicts and can never green. Any positive failure is
    ``red``. Advisory: a non-green verdict is information, never a block."""
    kind = result.kind
    if not result.executed:
        return GreenVerdict(False, "not_executed", kind)
    if result.verdict in _FAIL_VERDICTS:
        return GreenVerdict(False, "red", kind)
    if result.verdict not in _PASS_VERDICTS:
        # unavailable / unknown / skipped / executed_no_tests / partial —
        # no positive pass evidence.
        return GreenVerdict(False, "unavailable", kind)
    # verdict is a positive pass from here.
    fresh = (
        result.graph_revision == plan.graph_revision
        and result.patch_revision == plan.patch_revision
    )
    if not fresh:
        return GreenVerdict(False, "stale", kind)
    attribution_ok = result.attribution_requirement == "none" or result.attribution_satisfied
    if not attribution_ok:
        return GreenVerdict(False, "attribution_unmet", kind)
    # F4 MEMBERSHIP: every claimed covered entity/obligation must be one the PLAN
    # names (covered_entities ⊆ changed_entities ∪ obligations; same for
    # obligations). A result claiming coverage of a phantom entity — even with
    # matching revisions — is an unmapped claim, never green.
    plan_named = set(plan.changed_entities) | set(plan.obligations)
    claimed_ents = set(result.covered_entities)
    claimed_obl = set(result.covered_obligations)
    if not claimed_ents <= plan_named or not claimed_obl <= plan_named:
        return GreenVerdict(False, "executed_unmapped", kind)
    if len(claimed_ents) + len(claimed_obl) < 1:
        return GreenVerdict(False, "executed_unmapped", kind)
    return GreenVerdict(True, "green", kind)
