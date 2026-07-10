#!/usr/bin/env python3
"""TTD fixture test for scripts/build_eval_corpus.py — the stratified EVAL corpus builder.

This corpus is EVALUATION / CALIBRATION data ONLY — explicitly NOT training data.

RED-first: hand-crafts a live-lite-shaped jsonl (3 rows across strata) and a tiny tmp git
history yielding exactly ONE mined (inverse-patch) task, then asserts:
  * the extended stratum classifier (reuses groundtruth.pretask.stratum + 2 conservative
    local rules: config_dependency / insufficient_evidence);
  * the redaction discipline strips gold/test/patch tokens from every row (LEAK LAW);
  * the repo-disjoint split with repo-level time ordering (older repos=calibration;
    task dates may straddle the boundary — repo-earliest granularity);
  * byte-identical determinism across two runs.

MUTATION handle: ``assign_splits(rows, enforce_repo_disjoint=False)`` drops the repo-atomic
guard so a repo whose tasks straddle the median date lands in BOTH splits — the disjointness
test then bites (mirrors gt_substitution_grader's ``enforce_precedence`` handle).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import build_eval_corpus as bec  # noqa: E402  (hard import -> RED before the script exists)


# ---------------------------------------------------------------------------
# FIXTURES — live-lite-shaped rows (public benchmark shape), 3 strata, with
# DELIBERATE leak tokens to prove redaction + gold-metadata exclusion.
# ---------------------------------------------------------------------------
_LIVELITE = [
    {  # -> traceback (base classifier stratum C)
        "instance_id": "acme__widget-101",
        "repo": "acme/widget",
        "base_commit": "aaaa1111",
        "created_at": "2023-01-15T10:00:00.000",
        "problem_statement": (
            "Crash on startup\n\nThe app dies immediately.\n\n"
            "Traceback (most recent call last):\n"
            '  File "app.py", line 3, in <module>\n'
            "ValueError: boom\n"
        ),
        # gold metadata that must NEVER reach a row:
        "patch": "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n-x\n+y\n",
        "test_patch": "diff --git a/tests/test_app.py b/tests/test_app.py\n@@ +1 @@\n",
        "FAIL_TO_PASS": ["tests/test_app.py::test_startup"],
        "PASS_TO_PASS": [],
    },
    {  # -> config_dependency (my conservative rule; base B, manifest subject)
        "instance_id": "acme__widget-102",
        "repo": "acme/widget",
        "base_commit": "bbbb2222",
        "created_at": "2025-06-20T12:00:00.000",
        "problem_statement": (
            "Wrong dependency pin in requirements.txt\n\n"
            "The pinned version constraint is incompatible; pip install fails to "
            "resolve the environment for the release build.\n"
        ),
        "patch": "diff --git a/requirements.txt b/requirements.txt\n@@ @@\n",
        "test_patch": "",
        "FAIL_TO_PASS": ["tests/test_deps.py::test_pin"],
        "PASS_TO_PASS": [],
    },
    {  # -> behavioral (base B, no traceback, no config, carries leak tokens in prose)
        "instance_id": "beta__gizmo-7",
        "repo": "beta/gizmo",
        "base_commit": "cccc3333",
        "created_at": "2024-03-01T09:00:00.000",
        "problem_statement": (
            "Cache returns stale values under concurrent writes.\n\n"
            "Reproduce by hammering the writer; the reader sees an old value. "
            "Our test_stale_read repro and the diff --git snippet\n"
            "@@ -10,3 +10,4 @@\n show FAIL_TO_PASS behaviour.\n"
        ),
        "patch": "diff --git a/cache.py b/cache.py\n@@ @@\n",
        "test_patch": "",
        "FAIL_TO_PASS": ["tests/test_cache.py::test_stale_read"],
        "PASS_TO_PASS": [],
    },
]

# tokens no built row may ever contain (LEAK LAW).
_FORBIDDEN = ("test_", "FAIL_TO_PASS", "PASS_TO_PASS", "diff --git", "@@ ", "+++ ", "--- ")


@pytest.fixture()
def livelite_jsonl(tmp_path):
    p = tmp_path / "live_lite_mini.jsonl"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for row in _LIVELITE:
            fh.write(json.dumps(row) + "\n")
    return str(p)


def _git(repo, *args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("GIT_AUTHOR_NAME", "T")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@e")
    env.setdefault("GIT_COMMITTER_NAME", "T")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@e")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["git", "-C", repo, *args], check=True, env=env,
                          capture_output=True, text=True, encoding="utf-8")


@pytest.fixture()
def mined_repo(tmp_path):
    """A tmp git repo: base commit + ONE fix commit touching a .py file. The fix
    message names a gold path + a test id, which redaction must scrub."""
    repo = tmp_path / "MyProj"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m", "initial import",
         env_extra={"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"})
    (repo / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m",
         "fix: off-by-one in src/mod.py breaks test_addition for large inputs",
         env_extra={"GIT_AUTHOR_DATE": "2026-02-02T00:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2026-02-02T00:00:00+00:00"})
    return str(repo)


# ---------------------------------------------------------------------------
# EXTENDED CLASSIFIER (reuse + 2 local rules)
# ---------------------------------------------------------------------------
def test_classify_traceback():
    assert bec.classify_extended(_LIVELITE[0]["problem_statement"]) == "traceback"


def test_classify_config_dependency():
    assert bec.classify_extended(_LIVELITE[1]["problem_statement"]) == "config_dependency"


def test_classify_behavioral_default():
    assert bec.classify_extended(_LIVELITE[2]["problem_statement"]) == "behavioral"


def test_classify_insufficient_evidence():
    assert bec.classify_extended("Fix it.") == "insufficient_evidence"
    assert bec.classify_extended("") == "insufficient_evidence"


def test_classify_config_does_not_steal_traceback():
    # a traceback that also mentions requirements.txt stays traceback (C precedence).
    txt = ("Boom\nTraceback (most recent call last):\n  File \"x.py\", line 1\n"
           "ImportError: bad requirements.txt pin\n")
    assert bec.classify_extended(txt) == "traceback"


# ---------------------------------------------------------------------------
# REDACTION / LEAK LAW
# ---------------------------------------------------------------------------
def test_redaction_strips_leak_tokens():
    dirty = ("See test_stale_read and diff --git a/cache.py b/cache.py\n"
             "@@ -1 +1 @@\nFAIL_TO_PASS here, path src/mod.py::TestX::test_y\n")
    clean = bec.redact_statement(dirty, redact_paths=True)
    for tok in _FORBIDDEN:
        assert tok not in clean, f"redaction left {tok!r}: {clean!r}"
    assert "src/mod.py" not in clean  # gold path scrubbed when redact_paths=True


def test_redaction_keeps_prose_words():
    # bare word 'test' (no identifier underscore) survives — only identifiers scrub.
    kept = bec.redact_statement("please add a test and a version bump", redact_paths=False)
    assert "test" in kept and "version" in kept


# ---------------------------------------------------------------------------
# LIVELITE ROW BUILD
# ---------------------------------------------------------------------------
def test_livelite_rows_built(livelite_jsonl):
    rows = bec.iter_livelite_rows(livelite_jsonl)
    assert len(rows) == 3
    strata = {r["task_id"]: r["stratum"] for r in rows}
    assert strata["acme__widget-101"] == "traceback"
    assert strata["acme__widget-102"] == "config_dependency"
    assert strata["beta__gizmo-7"] == "behavioral"
    for r in rows:
        assert r["source"] == "livelite"
        assert set(r.keys()) == {"task_id", "source", "repo", "base_sha", "date",
                                 "problem_statement", "stratum", "split", "provenance"}
        # gold-metadata fields must not survive as keys anywhere in the row
        blob = json.dumps(r)
        for tok in _FORBIDDEN:
            assert tok not in blob, f"leak {tok!r} in {r['task_id']}"


# ---------------------------------------------------------------------------
# MINED (inverse-patch) ROW BUILD
# ---------------------------------------------------------------------------
def test_mined_one_task(mined_repo):
    rows = bec.iter_mined_rows(mined_repo)
    assert len(rows) == 1, [r["task_id"] for r in rows]
    r = rows[0]
    assert r["source"] == "mined"
    assert r["repo"] == "MyProj"
    # base_sha is the PARENT snapshot (pre-fix), never the fix commit itself.
    parent = _git(mined_repo, "rev-parse", "HEAD^").stdout.strip()
    assert r["base_sha"] == parent
    # statement reconstructed from the message, NOT the diff; gold path + test id scrubbed.
    ps = r["problem_statement"]
    assert "off-by-one" in ps
    assert "src/mod.py" not in ps
    for tok in _FORBIDDEN:
        assert tok not in json.dumps(r)


def test_mined_skips_nonfix_and_root(mined_repo):
    # only the single fix commit qualifies (root 'initial import' is not a fix + has no parent)
    rows = bec.iter_mined_rows(mined_repo)
    assert {r["problem_statement"].splitlines()[0].startswith("fix") for r in rows} == {True}


# ---------------------------------------------------------------------------
# SPLIT — repo-disjoint, repo-level time ordering (+ MUTATION)
# ---------------------------------------------------------------------------
def _split_fixture_rows():
    # repoX straddles the global median date; repoY is old; repoZ is new.
    return [
        {"task_id": "x-old", "repo": "X", "date": "2020-01-01T00:00:00", "source": "s"},
        {"task_id": "x-new", "repo": "X", "date": "2026-01-01T00:00:00", "source": "s"},
        {"task_id": "y-old", "repo": "Y", "date": "2019-01-01T00:00:00", "source": "s"},
        {"task_id": "z-new", "repo": "Z", "date": "2027-01-01T00:00:00", "source": "s"},
    ]


def test_split_repo_disjoint_and_time_ordered():
    res = bec.assign_splits(_split_fixture_rows(), enforce_repo_disjoint=True)
    assert res["repo_overlap"] == [], res
    # each repo lands wholly in one split
    by_repo = {}
    for r in res["rows"]:
        by_repo.setdefault(r["repo"], set()).add(r["split"])
    for repo, splits in by_repo.items():
        assert len(splits) == 1, f"{repo} straddles splits: {splits}"
    # older repos -> calibration, newer -> eval (Y oldest in calibration; Z newest in eval)
    place = {r["repo"]: r["split"] for r in res["rows"]}
    assert place["Y"] == "calibration"
    assert place["Z"] == "eval"


def test_split_mutation_bites():
    # MUTATION: per-task median split (no repo-atomic guard) -> repoX straddles -> overlap.
    good = bec.assign_splits(_split_fixture_rows(), enforce_repo_disjoint=True)
    assert good["repo_overlap"] == []
    mutant = bec.assign_splits(_split_fixture_rows(), enforce_repo_disjoint=False)
    assert mutant["repo_overlap"], "mutation did not bite: repos still disjoint"
    assert "X" in mutant["repo_overlap"]


# ---------------------------------------------------------------------------
# FULL BUILD — leak law across every row, provenance counts, determinism
# ---------------------------------------------------------------------------
def test_build_corpus_leak_law(livelite_jsonl, mined_repo):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    assert corpus["rows"], "no rows built"
    for r in corpus["rows"]:
        blob = json.dumps(r)
        for tok in _FORBIDDEN:
            assert tok not in blob, f"LEAK {tok!r} in {r['task_id']}"
        assert r["split"] in ("calibration", "eval")
        assert r["stratum"] in bec.STRATA


def test_provenance_counts(livelite_jsonl, mined_repo):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    prov = corpus["provenance"]
    assert prov["classification_rules_version"]
    assert prov["total_rows"] == len(corpus["rows"])
    # counts nested per split per stratum reconcile to the total
    tot = 0
    for split, strata in prov["counts_per_split_per_stratum"].items():
        assert split in ("calibration", "eval")
        for stratum, n in strata.items():
            assert stratum in bec.STRATA
            tot += n
    assert tot == prov["total_rows"]
    assert prov["split_boundary"]["n_repos"] >= 1


def test_leak_scan_invariant_holds(livelite_jsonl, mined_repo):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    scan = corpus["provenance"]["leak_scan"]
    assert scan["invariant_holds"] is True, scan["enforced_zero_counts"]
    for k, v in scan["enforced_zero_counts"].items():
        assert v == 0, f"gold-leak {k}={v}"


def test_determinism_two_runs(livelite_jsonl, mined_repo, tmp_path):
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    c1 = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    c2 = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    bec.write_outputs(c1, str(out1))
    bec.write_outputs(c2, str(out2))
    for name in ("corpus.jsonl", "PROVENANCE.json"):
        a = (out1 / name).read_bytes()
        b = (out2 / name).read_bytes()
        assert a == b, f"{name} not byte-identical across runs"


# ---------------------------------------------------------------------------
# F1 — bare-basename + test-file-class redaction (LIPI review, BLOCKING)
# ---------------------------------------------------------------------------
def test_redaction_bare_basename_mined():
    # slashless gold basename in a mined commit message MUST be redacted.
    out = bec.redact_statement("fix walker.go stem check", redact_paths=True)
    assert "walker.go" not in out and "[path]" in out, out
    out2 = bec.redact_statement("fix: wrong pin in syncer_csa.go and setup.py",
                                redact_paths=True)
    assert "syncer_csa.go" not in out2 and "setup.py" not in out2, out2
    # public livelite prose keeps NON-test bare filenames verbatim (paths not redacted).
    keep = bec.redact_statement("fix walker.go stem check", redact_paths=False)
    assert "walker.go" in keep


def test_redaction_test_file_classes():
    # test-FILE names are leak-law targets in BOTH sources (redact_paths=False too).
    cases = {
        "see basicAuth.spec.js failing": "basicAuth",
        "regression in FooTest.java here": "FooTest",
        "our parser_spec.rb covers it": "parser_spec",
        "run test_utils.py again": "test_utils",
        "storages_test.go is red": "storages_test",
    }
    for txt, residue in cases.items():
        out = bec.redact_statement(txt, redact_paths=False)
        assert "[test-id]" in out, (txt, out)
        assert residue not in out, (txt, out)


# ---------------------------------------------------------------------------
# F2 — the seam: build_corpus MUST wire the guard; write_outputs MUST fail closed
# ---------------------------------------------------------------------------
def test_build_corpus_split_guard_wired(livelite_jsonl, mined_repo):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    prov = corpus["provenance"]
    assert prov["repo_overlap"] == [], prov["repo_overlap"]
    assert prov["split_boundary"]["rule"].startswith("repo-atomic"), prov["split_boundary"]
    assert prov["split_boundary"]["rule_granularity"] == \
        "repo-earliest, task dates may straddle"


def test_write_outputs_fails_closed_on_leak(livelite_jsonl, mined_repo, tmp_path):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    corpus["rows"][0]["problem_statement"] += "\nFAIL_TO_PASS"
    with pytest.raises(ValueError, match="leak"):
        bec.write_outputs(corpus, str(tmp_path / "poisoned"))


# ---------------------------------------------------------------------------
# F3 — repo-identity normalization + livelite-twin intersection
# ---------------------------------------------------------------------------
def _mk_fix_repo(base, name, remote, fix_date):
    repo = base / name
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m", "initial",
         env_extra={"GIT_AUTHOR_DATE": "2025-01-01T00:00:00+00:00",
                    "GIT_COMMITTER_DATE": "2025-01-01T00:00:00+00:00"})
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m", "fix: core bug",
         env_extra={"GIT_AUTHOR_DATE": fix_date, "GIT_COMMITTER_DATE": fix_date})
    if remote:
        _git(str(repo), "remote", "add", "origin", remote)
    return str(repo)


def test_repo_identity_shared_remote(tmp_path):
    # two checkouts, different dir names, SAME upstream (https + ssh URL forms) ->
    # ONE identity -> one atomic split unit (no by-luck disjointness).
    a = _mk_fix_repo(tmp_path, "cloneA", "https://github.com/acme/onething.git",
                     "2026-02-02T00:00:00+00:00")
    b = _mk_fix_repo(tmp_path, "cloneB", "git@github.com:acme/onething.git",
                     "2026-03-03T00:00:00+00:00")
    rows = bec.iter_mined_rows(a) + bec.iter_mined_rows(b)
    assert len(rows) == 2, [r["task_id"] for r in rows]
    assert {r["repo"] for r in rows} == {"acme/onething"}, {r["repo"] for r in rows}
    res = bec.assign_splits(rows, enforce_repo_disjoint=True)
    assert res["repo_overlap"] == []
    assert len({r["split"] for r in res["rows"]}) == 1  # one identity -> one split


def test_twin_dropped_by_gold_sha(tmp_path, mined_repo):
    # a mined row whose fix_commit IS a livelite task's gold commit is a TWIN -> dropped.
    fix_sha = _git(mined_repo, "rev-parse", "HEAD").stdout.strip()
    twin = {
        "instance_id": "acme__widget-900",
        "repo": "acme/widget",
        "base_commit": "dddd4444",
        "created_at": "2025-05-05T00:00:00.000",
        "problem_statement": ("Something breaks when the widget reloads its saved state "
                              "after an unexpected restart of the host process."),
        "commit_urls": [f"https://github.com/acme/widget/commit/{fix_sha}"],
        "pull_number": "900",
    }
    p = tmp_path / "ll_twin.jsonl"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(twin) + "\n")
    corpus = bec.build_corpus(livelite_path=str(p), history_repos=[mined_repo])
    ids = {r["task_id"] for r in corpus["rows"]}
    assert not any(t.startswith("mined__") for t in ids), ids
    dropped = corpus["provenance"]["dropped_twins"]
    assert dropped and dropped[0]["matched_instance_id"] == "acme__widget-900", dropped
    assert "fix_commit" in dropped[0]["reason"]


def test_pr_ref_extraction():
    assert bec.extract_pr_refs("fix: x (#12), also see (#34)") == ["12", "34"]
    assert bec.extract_pr_refs("no refs here #99") == []


# ---------------------------------------------------------------------------
# F4 — PROVENANCE must reproduce the build
# ---------------------------------------------------------------------------
def test_provenance_reproducibility(livelite_jsonl, mined_repo):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    src = corpus["provenance"]["sources"]
    assert re.fullmatch(r"[0-9a-f]{64}", src["livelite_sha256"]), src
    assert src["max_mined"] == 0
    hr = src["history_repos"]
    assert len(hr) == 1, hr
    assert os.path.isabs(hr[0]["path"]), hr[0]
    assert re.fullmatch(r"[0-9a-f]{40}", hr[0]["head_sha"]), hr[0]
    assert hr[0]["identity"] == "MyProj"  # no origin remote -> basename fallback


# ---------------------------------------------------------------------------
# F5/F6 — honesty notes + code-signal override of the brevity floors
# ---------------------------------------------------------------------------
def test_provenance_honesty_notes(livelite_jsonl, mined_repo):
    prov = bec.build_corpus(livelite_path=livelite_jsonl,
                            history_repos=[mined_repo])["provenance"]
    assert "abstention" in prov["insufficient_evidence_retention"]
    assert "fix_commit" in prov["task_facing_export_hazard"]


def test_code_signal_overrides_insufficient():
    # a short title naming a concrete identifier is LOCALIZABLE, never insufficient
    # (the weasyprint "Support `unicode-range`" precedence-theft case).
    assert bec.classify_extended("Support `unicode-range`") == "behavioral"
    # pure-noise subjects with no code signal remain insufficient.
    assert bec.classify_extended("fix: lint") == "insufficient_evidence"
    assert bec.classify_extended("Fix typo") == "insufficient_evidence"


# ---------------------------------------------------------------------------
# R1 — write_outputs must recompute the overlap from the rows BEING WRITTEN,
# never trust the (possibly stale) provenance field.
# ---------------------------------------------------------------------------
def test_write_outputs_fails_closed_on_stale_overlap(livelite_jsonl, mined_repo, tmp_path):
    corpus = bec.build_corpus(livelite_path=livelite_jsonl, history_repos=[mined_repo])
    assert corpus["provenance"]["repo_overlap"] == []  # provenance says clean...
    # ...the reviewer's exact escape: flip ONE row's split post-build. acme/widget has
    # two rows, so the flip makes it straddle both splits while provenance stays stale.
    row = next(r for r in corpus["rows"] if r["task_id"] == "acme__widget-101")
    row["split"] = "eval" if row["split"] == "calibration" else "calibration"
    with pytest.raises(ValueError, match="overlap"):
        bec.write_outputs(corpus, str(tmp_path / "stale_overlap"))


# ---------------------------------------------------------------------------
# R2 — extensionless SPECIFIER leak (conventional-commit scopes / URL paths):
# "fix(utils/mime):" names the gold file src/utils/mime.ts without an extension.
# ---------------------------------------------------------------------------
def test_redaction_extensionless_specifier_mined():
    # (a) conventional-commit SCOPE form
    out = bec.redact_statement("fix(utils/mime): correct lookup table", redact_paths=True)
    assert "utils/mime" not in out and "[path]" in out, out
    # (b) standalone module/URL-path form (>=2 segments, no extension)
    out2 = bec.redact_statement("regression in src/runtime/context handling",
                                redact_paths=True)
    assert "src/runtime/context" not in out2 and "[path]" in out2, out2


def test_specifier_rule_is_conservative():
    # single words / single-segment scopes are stratum-A task input BY DESIGN — never
    # redacted (the grey stem/module class stays).
    keep = bec.redact_statement("fix(router): dispatch order regression", redact_paths=True)
    assert "router" in keep, keep
    # common English slash-pairs survive via the documented stoplist.
    prose = bec.redact_statement("choose and/or drop I/O retries in CI/CD",
                                 redact_paths=True)
    assert "and/or" in prose and "I/O" in prose and "CI/CD" in prose, prose
    # numeric-segment tokens (dates, versions) never match: segments must start with
    # a letter/underscore.
    dated = bec.redact_statement("regressed on 2026/07/09 in prod", redact_paths=True)
    assert "2026/07/09" in dated, dated
    # livelite prose (redact_paths=False) keeps specifiers verbatim.
    ll = bec.redact_statement("fix(utils/mime): correct lookup", redact_paths=False)
    assert "utils/mime" in ll, ll
