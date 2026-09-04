"""BENCHMARK-BLOCKER red->green tests for scripts/verify/deepswe_outcome.py.

Fixtures are the REAL 4-task PATH A artifacts (run 27290157847, copied verbatim into
tests/fail_closed/fixtures/pathA_deepswe_nonpython_27290157847/):

BLOCKER 1 (G4, gt_gt.md SS12:764): `GRAPH_FAIL_MISSING_HANDOFF` is a KNOWN FALSE FAIL --
the graph certificate is written PRE-AGENT and cannot see the handoff. When the runtime
witness holds (gt_prebuilt_active=true AND hook_hash_match=true), the classifier must
NOT stamp failure_class="GT" off that cert; it falls through to the real outcome ladder.
All 4 PATH A tasks carry exactly this shape and were mislabeled GT. A GENUINE cert fail
(any other verdict, or no witness) still classifies GT -- only the one reconcilable
verdict is skipped, fail-closed everywhere else.

BLOCKER 2: the id-finder returned null for all 4 tasks (pier per-trial result.json has
no `instance_id`/`info` -- the identity lives in `task_name` ("org/<slug>"),
`task_id.path` (".../tasks/<slug>"), and the trial dir name ("<slug>__<hash>",
TRUNCATED to ~32 chars so it ranks last). instance_id=null makes the paired Wilcoxon
impossible. The extractor must resolve the slug from the available source, while an
explicit SWE-bench instance_id (which legitimately contains `__`) stays verbatim.

No task-specific logic in the fixes -- the fixtures only PROVE the generalized rules.
"""

import copy
import importlib.util
import json
import os

import pytest

_DO_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "verify", "deepswe_outcome.py"
)
_spec = importlib.util.spec_from_file_location("deepswe_outcome_b", _DO_PATH)
do = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(do)

FIXTURE_ROOT = os.path.join(
    os.path.dirname(__file__), "fixtures", "pathA_deepswe_nonpython_27290157847"
)
TASKS = (
    "abs-module-cache-flags",
    "csstree-shorthand-expansion-compression",
    "arktype-json-schema-refs-dependencies",
    "boa-hierarchical-evaluation-cancellation",
)


def _real_outcome_rec(task: str) -> dict:
    with open(os.path.join(FIXTURE_ROOT, task, "outcome.json"), encoding="utf-8") as fh:
        rec = json.load(fh)["tasks"][0]
    # Strip the STORED (wrong) classification; the test re-classifies the raw signals.
    rec = copy.deepcopy(rec)
    rec.pop("failure_class", None)
    rec.pop("in_resolved_denominator", None)
    return rec


# ===========================================================================
# BLOCKER 1 -- SS12 witness reconciliation of GRAPH_FAIL_MISSING_HANDOFF
# ===========================================================================


@pytest.mark.parametrize("task", TASKS)
def test_known_false_fail_with_witness_is_not_gt(task):
    """REAL artifact: cert GRAPH_FAIL_MISSING_HANDOFF + witness true/true must NOT be GT.

    gt_gt.md SS12:764 -- the cert is pre-agent; the runtime witness
    (gt_prebuilt_active AND hook_hash_match) PROVES the handoff. With reward=0 and
    n_agent_steps>0 the real outcome ladder lands on AGENT (sound GT context, model
    missed), exactly what the GT/AGENT attribution needs to be citable.
    """
    rec = _real_outcome_rec(task)
    assert rec["cert_verdicts"]["graph_certificate.json"]["verdict"] == "GRAPH_FAIL_MISSING_HANDOFF"
    assert rec["gt_prebuilt_active"] is True
    assert rec["hook_hash_match"] is True
    assert rec["cert_fail"] is True  # the raw signal stays honest
    got = do.classify_outcome(rec)
    assert got != "GT", f"{task}: SS12 witness must reconcile the known false fail"
    assert got == "AGENT"


def test_genuine_missing_handoff_without_witness_still_gt():
    """Same cert verdict but NO witness -> the false-fail cannot be reconciled -> GT."""
    rec = _real_outcome_rec(TASKS[0])
    rec["gt_prebuilt_active"] = None
    rec["hook_hash_match"] = None
    assert do.classify_outcome(rec) == "GT"


def test_genuine_missing_handoff_with_half_witness_still_gt():
    """hook_hash_match false/absent breaks the witness -> still GT (fail-closed)."""
    rec = _real_outcome_rec(TASKS[0])
    rec["hook_hash_match"] = None
    assert do.classify_outcome(rec) == "GT"


def test_other_cert_fail_with_witness_still_gt():
    """ONLY GRAPH_FAIL_MISSING_HANDOFF is reconcilable. Any other failing verdict
    (e.g. a real embedder fail) classifies GT even with the witness true/true."""
    rec = _real_outcome_rec(TASKS[0])
    rec["cert_verdicts"] = {
        "embedder_certificate.json": {
            "verdict": "EMBEDDER_FAIL_ZERO_MODEL",
            "pass": False,
            "is_fail": True,
        }
    }
    rec["cert_fail"] = True
    assert do.classify_outcome(rec) == "GT"


def test_mixed_cert_fails_with_witness_still_gt():
    """A reconcilable verdict + a genuine one together: the genuine fail wins -> GT."""
    rec = _real_outcome_rec(TASKS[0])
    rec["cert_verdicts"]["lsp_certificate.json"] = {
        "verdict": "LSP_FAIL_NO_SERVER",
        "pass": False,
        "is_fail": True,
    }
    assert do.classify_outcome(rec) == "GT"


def test_cert_fail_without_verdict_detail_stays_gt():
    """cert_fail=True with NO cert_verdicts detail: cannot verify the verdict is the
    reconcilable one -> fail-closed GT (preserves the synthetic-record contract the
    existing test_classify_gt_cert_fail asserts)."""
    rec = {
        "infra_markers": [],
        "adapter_fail": False,
        "gt_prebuilt_active": True,
        "hook_hash_match": True,
        "cert_fail": True,
        "reward": 0.0,
        "n_agent_steps": 8,
    }
    assert do.classify_outcome(rec) == "GT"


def test_reconciled_false_fail_with_reward_is_resolved():
    """Reconciled false-fail + reward 1.0 -> RESOLVED (the ladder runs normally)."""
    rec = _real_outcome_rec(TASKS[0])
    rec["reward"] = 1.0
    assert do.classify_outcome(rec) == "RESOLVED"


# ===========================================================================
# BLOCKER 2 -- instance_id pairing key from the pier per-trial result shape
# ===========================================================================


def _real_result(task: str) -> tuple[dict, str]:
    with open(os.path.join(FIXTURE_ROOT, task, "result.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    with open(os.path.join(FIXTURE_ROOT, task, "trial_dir_name.txt"), encoding="utf-8") as fh:
        trial_dir = fh.read().strip()
    return d, trial_dir


@pytest.mark.parametrize("task", TASKS)
def test_instance_id_resolves_from_real_trial_result(task):
    """REAL artifact: the pier per-trial result.json has no instance_id/info — the
    identity lives in task_name ('datacurve/<slug>'). The extractor must return the
    task slug (the run-set / pairing key), never None."""
    d, trial_dir = _real_result(task)
    assert d.get("instance_id") is None and d.get("info") is None  # why it was null
    info = d.get("info") or {}
    iid = do.extract_instance_id(d, info, trial_dir=trial_dir)
    assert iid == task, f"{task}: pairing key must be the task slug, got {iid!r}"


def test_instance_id_explicit_swebench_id_kept_verbatim():
    """An explicit instance_id (SWE-bench shape, legitimately containing `__`) must be
    returned VERBATIM — never slug-normalized or `__`-split."""
    d = {"instance_id": "astropy__astropy-12907", "task_name": "datacurve/whatever"}
    assert do.extract_instance_id(d, {}, trial_dir=None) == "astropy__astropy-12907"
    info = {"instance": {"instance_id": "django__django-11099"}}
    assert do.extract_instance_id({}, info, trial_dir=None) == "django__django-11099"


def test_instance_id_task_id_path_fallback():
    """Without task_name, task_id.path ('.../tasks/<slug>') yields the slug."""
    d = {"task_id": {"path": "deepswe-bench/tasks/boa-hierarchical-evaluation-cancellation"}}
    assert (
        do.extract_instance_id(d, {}, trial_dir=None) == "boa-hierarchical-evaluation-cancellation"
    )


def test_instance_id_trial_dir_last_resort_strips_attempt_hash():
    """Last resort: the trial dir name '<slug>__<hash>' — strip ONLY the trailing
    attempt hash. (It ranks last because pier truncates the slug in the dir name.)"""
    assert (
        do.extract_instance_id({}, {}, trial_dir="jobs/2026-06-10__16-29-15/mytask__AbC123")
        == "mytask"
    )
    d = {"trial_name": "sometask__ZPXAd5C"}
    assert do.extract_instance_id(d, {}, trial_dir=None) == "sometask"


def test_instance_id_none_when_nothing_available():
    """No identity source at all -> None (surfaced, never fabricated)."""
    assert do.extract_instance_id({}, {}, trial_dir=None) is None


def test_build_signal_record_marks_reconciliation(tmp_path):
    """End-to-end: a pre-agent GRAPH_FAIL_MISSING_HANDOFF cert + a witness-true trial
    log -> record carries cert_fail=True (raw) + cert_fail_reconciled=True (derived)
    and classifies AGENT, not GT."""
    cert_dir = tmp_path / "gt"
    cert_dir.mkdir()
    (cert_dir / "graph_certificate.json").write_text(
        '{"verdict": "GRAPH_FAIL_MISSING_HANDOFF", "pass": null}', encoding="utf-8"
    )
    log = (
        "[GT_META] graph_witness ... | gt_prebuilt_active=true; "
        "hook_graph_hash_matches_post_lsp=True; substrate_digest=sha256:deadbeef\n"
    )
    rec = do.build_signal_record(
        instance_id="some-task",
        reward=0.0,
        n_agent_steps=42,
        exit_status="Submitted",
        trial_log=log,
        cert_dir=str(cert_dir),
    )
    assert rec["cert_fail"] is True
    assert rec["cert_fail_reconciled"] is True
    assert rec["failure_class"] == "AGENT"
