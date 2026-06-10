"""Stage 5 — DeepSWE outcome failure-classification tests.

Prove the deterministic classifier in scripts/verify/deepswe_outcome.py assigns the
RIGHT failure_class to each of the four classes (INFRA / GT / AGENT / RESOLVED) from
synthetic per-task signal records, that INFRA (and UNKNOWN) are EXCLUDED from the
resolved-rate denominator, and that the paired-delta scaffold is structure-only (no
fabricated baseline). No SWE-bench tasks, no gold, no per-task IDs, no per-repo logic —
the classifier is generalized rules over signals, identical for all 113 tasks.
"""
import importlib.util
import os

_DO_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "verify", "deepswe_outcome.py"
)
_spec = importlib.util.spec_from_file_location("deepswe_outcome_t", _DO_PATH)
do = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(do)


# ── classify_outcome: one synthetic record per class ────────────────────────

def test_classify_infra_substrate_pull_fail():
    # §E infra marker present in the trial log -> INFRA, regardless of any later signal.
    rec = {
        "infra_markers": ["GT_SUBSTRATE_PULL_FAIL"],
        "adapter_fail": False, "gt_prebuilt_active": True, "hook_hash_match": True,
        "cert_fail": False, "reward": 1.0, "n_agent_steps": 12,
    }
    assert do.classify_outcome(rec) == "INFRA"


def test_classify_infra_eval_no_report():
    # Harness produced no report -> INFRA (harness crash), excluded from denominator.
    rec = {"infra_markers": [], "eval_no_report": True, "reward": None, "n_agent_steps": None}
    assert do.classify_outcome(rec) == "INFRA"


def test_classify_infra_every_marker():
    # Every §E infra marker must classify INFRA (generalized, identical rule).
    for marker in do.INFRA_LOG_MARKERS:
        rec = {"infra_markers": [marker], "reward": 0.0, "n_agent_steps": 5}
        assert do.classify_outcome(rec) == "INFRA", marker


def test_classify_gt_adapter_fail():
    # DEEPSWE_ADAPTER_FAIL -> GT (adapter could not wire/consume the substrate).
    rec = {
        "infra_markers": [], "adapter_fail": True, "gt_prebuilt_active": None,
        "hook_hash_match": None, "cert_fail": False, "reward": 0.0, "n_agent_steps": 0,
    }
    assert do.classify_outcome(rec) == "GT"


def test_classify_gt_prebuilt_inactive():
    # gt_prebuilt_active=false -> GT (substrate graph was not consumed).
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": False,
        "hook_hash_match": None, "cert_fail": False, "reward": 0.0, "n_agent_steps": 8,
    }
    assert do.classify_outcome(rec) == "GT"


def test_classify_gt_hash_mismatch():
    # hook_graph_hash != post-LSP hash -> GT (consumed graph diverges from substrate).
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": True,
        "hook_hash_match": False, "cert_fail": False, "reward": 0.0, "n_agent_steps": 8,
    }
    assert do.classify_outcome(rec) == "GT"


def test_classify_gt_cert_fail():
    # Any embedder/LSP/graph cert FAIL -> GT (unsound substrate delivered).
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": True,
        "hook_hash_match": True, "cert_fail": True, "reward": 0.0, "n_agent_steps": 8,
    }
    assert do.classify_outcome(rec) == "GT"


def test_classify_resolved():
    # reward 1.0 with sound GT context -> RESOLVED.
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": True,
        "hook_hash_match": True, "cert_fail": False, "reward": 1.0, "n_agent_steps": 14,
    }
    assert do.classify_outcome(rec) == "RESOLVED"


def test_classify_agent():
    # Sound GT context (prebuilt active, valid certs, hash match), agent ran, reward 0 -> AGENT.
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": True,
        "hook_hash_match": True, "cert_fail": False, "reward": 0.0, "n_agent_steps": 20,
    }
    assert do.classify_outcome(rec) == "AGENT"


def test_classify_unknown_no_signal():
    # No trial result + no infra marker -> UNKNOWN (un-attributable, surfaced not bucketed).
    rec = {"infra_markers": [], "reward": None, "n_agent_steps": None}
    assert do.classify_outcome(rec) == "UNKNOWN"


# ── P1-h: missing witness on a ran-but-unresolved task classifies GT ─────────

def test_classify_gt_missing_witness_ran_unresolved():
    # Agent RAN (steps>0), did NOT resolve (reward<1), and the consumption witness is
    # MISSING (gt_prebuilt_active unknown): witness-absent = unproven consumption =
    # GT's problem — class GT, IN the resolved denominator (never UNKNOWN-excluded).
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": None,
        "hook_hash_match": None, "cert_fail": False, "reward": 0.0, "n_agent_steps": 17,
    }
    assert do.classify_outcome(rec) == "GT"
    assert do.is_in_resolved_denominator("GT") is True


def test_classify_missing_witness_resolved_stays_resolved():
    # reward 1.0 wins before the witness-absent rule (precedence 3 > 3b).
    rec = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": None,
        "cert_fail": False, "reward": 1.0, "n_agent_steps": 9,
    }
    assert do.classify_outcome(rec) == "RESOLVED"


def test_classify_missing_witness_no_reward_stays_unknown():
    # UNKNOWN is kept ONLY for no-result-at-all: steps>0 but NO reward at all.
    rec = {"infra_markers": [], "gt_prebuilt_active": None, "reward": None, "n_agent_steps": 9}
    assert do.classify_outcome(rec) == "UNKNOWN"


# ── P1-h: infra-marker token collision (line-anchored matching) ──────────────

def test_find_infra_markers_line_anchored():
    # The workflow's own fail-closed echo (marker starts the line) -> detected.
    log = "some output\nGT_ARTIFACT_MISSING / SUBSTRATE_MISSING_CERTS: /gt_artifacts/brief.txt absent\n"
    assert do.find_infra_markers(log) == ["GT_ARTIFACT_MISSING"]
    # GHA ::error:: prefix and leading whitespace are tolerated.
    assert do.find_infra_markers("  ::error::GT_RUN_PROOF_FAIL: boom\n") == ["GT_RUN_PROOF_FAIL"]


def test_find_infra_markers_embedded_token_not_matched():
    # An infra token EMBEDDED mid-line (not line-anchored) must NOT count as INFRA.
    log = "[GT_META] witness error=DEEPSWE_ADAPTER_FAIL(GT_ARTIFACT_MISSING: brief absent)\n"
    assert do.find_infra_markers(log) == []
    # Even without the adapter marker, a mid-line token is not the workflow's echo.
    assert do.find_infra_markers("note: saw GT_ARTIFACT_MISSING earlier\n") == []


def test_adapter_fail_line_with_embedded_infra_token_classifies_gt():
    # The collision case: the ONLY 'GT_ARTIFACT_MISSING' occurrence lives INSIDE a
    # DEEPSWE_ADAPTER_FAIL message. INFRA must NOT eat the adapter-consume failure.
    log = ("[GT_META] graph_witness ... | error=DEEPSWE_ADAPTER_FAIL: "
           "GT_ARTIFACT_MISSING brief.txt not consumed\n")
    rec = do.build_signal_record(
        instance_id="repo__e-5", reward=0.0, n_agent_steps=11, exit_status="Submitted",
        trial_log=log, cert_dir=None,
    )
    assert rec["infra_markers"] == []
    assert rec["adapter_fail"] is True
    assert rec["failure_class"] == "GT"


def test_line_anchored_infra_marker_still_classifies_infra():
    # A genuine workflow fail-closed line still wins as INFRA (precedence 1).
    log = "GT_SUBSTRATE_PULL_FAIL: docker pull of the pinned substrate failed\n"
    rec = do.build_signal_record(
        instance_id="repo__f-6", reward=0.0, n_agent_steps=0, exit_status=None,
        trial_log=log, cert_dir=None,
    )
    assert rec["infra_markers"] == ["GT_SUBSTRATE_PULL_FAIL"]
    assert rec["failure_class"] == "INFRA"


def test_issue_missing_marker_is_infra():
    # P0.1-a: the GT_ISSUE_MISSING fail-closed echo classifies INFRA (harness input),
    # excluded from the resolved denominator.
    assert "GT_ISSUE_MISSING" in do.INFRA_LOG_MARKERS
    log = "GT_ISSUE_MISSING: no issue text for this task — refusing to run the substrate\n"
    rec = do.build_signal_record(
        instance_id="repo__g-7", reward=None, n_agent_steps=None, exit_status=None,
        trial_log=log, cert_dir=None,
    )
    assert rec["failure_class"] == "INFRA"
    assert rec["in_resolved_denominator"] is False


def test_precedence_infra_beats_gt_beats_outcome():
    # INFRA wins over a GT signal; GT wins over a resolved reward.
    infra_over_gt = {"infra_markers": ["GT_RUN_PROOF_FAIL"], "adapter_fail": True, "reward": 1.0}
    assert do.classify_outcome(infra_over_gt) == "INFRA"
    gt_over_resolved = {
        "infra_markers": [], "adapter_fail": False, "gt_prebuilt_active": False,
        "cert_fail": False, "reward": 1.0, "n_agent_steps": 9,
    }
    assert do.classify_outcome(gt_over_resolved) == "GT"


# ── resolved-rate denominator: INFRA excluded ───────────────────────────────

def test_infra_excluded_from_denominator():
    assert do.is_in_resolved_denominator("INFRA") is False
    assert do.is_in_resolved_denominator("UNKNOWN") is False
    assert do.is_in_resolved_denominator("GT") is True
    assert do.is_in_resolved_denominator("AGENT") is True
    assert do.is_in_resolved_denominator("RESOLVED") is True


def test_tally_excludes_infra_from_denominator():
    # One of each class. Denominator = GT+AGENT+RESOLVED = 3 (INFRA and UNKNOWN excluded).
    records = [
        {"failure_class": "INFRA"},
        {"failure_class": "GT"},
        {"failure_class": "AGENT"},
        {"failure_class": "RESOLVED"},
        {"failure_class": "UNKNOWN"},
    ]
    t = do.tally_classes(records)
    assert t["total_tasks"] == 5
    assert t["denominator_excluding_infra"] == 3
    assert t["excluded_from_denominator"] == 2  # INFRA + UNKNOWN
    assert t["resolved"] == 1
    # resolved_rate = 1/3 over the infra-excluded denominator, 8-dp.
    assert t["resolved_rate"] == f"{(1 / 3):.8f}"


def test_tally_resolved_rate_does_not_count_infra_as_failure():
    # An INFRA-heavy run must NOT depress the resolved rate. 1 RESOLVED, 3 INFRA -> rate 1.0.
    records = [{"failure_class": "RESOLVED"}] + [{"failure_class": "INFRA"}] * 3
    t = do.tally_classes(records)
    assert t["denominator_excluding_infra"] == 1
    assert t["resolved_rate"] == f"{1.0:.8f}"


# ── paired-delta scaffolding: structure only, no fabricated baseline ─────────

def test_paired_delta_no_baseline_is_none():
    gt_on = {"instance_id": "repo__x-1", "failure_class": "AGENT", "reward": 0.0, "n_agent_steps": 20}
    p = do.build_paired_delta(gt_on, baseline=None)
    assert p["instance_id"] == "repo__x-1"
    assert p["baseline_present"] is False
    assert p["baseline"] is None
    assert p["resolved_delta"] is None
    assert p["action_count_delta"] is None


def test_paired_delta_with_baseline_computes_8dp_delta():
    gt_on = {"instance_id": "repo__x-1", "failure_class": "RESOLVED", "reward": 1.0, "n_agent_steps": 12}
    baseline = {"instance_id": "repo__x-1", "failure_class": "AGENT", "reward": 0.0, "n_agent_steps": 18}
    p = do.build_paired_delta(gt_on, baseline=baseline)
    assert p["baseline_present"] is True
    assert p["key_mismatch"] is False
    assert p["resolved_delta"] == f"{1.0:.8f}"
    assert p["action_count_delta"] == f"{(12 - 18):.8f}"  # -6.00000000


def test_paired_delta_flags_key_mismatch():
    gt_on = {"instance_id": "repo__x-1", "reward": 1.0}
    baseline = {"instance_id": "repo__y-2", "reward": 0.0}
    p = do.build_paired_delta(gt_on, baseline=baseline)
    assert p["key_mismatch"] is True


# ── end-to-end: build_signal_record parses log + certs, then classifies ──────

def test_build_signal_record_gt_from_meta_witness(tmp_path):
    # A trial log whose [GT_META] witness reports prebuilt_active=false -> GT.
    log = (
        "[GT_META] graph_witness host_resolved_graph_db=/tmp/gt/graph.db "
        "hook_graph_hash=abc _gt_prebuilt_active=False | gt_artifacts=/tmp/gt; "
        "gt_prebuilt_active=false; note=no_host_resolved_graph\n"
    )
    rec = do.build_signal_record(
        instance_id="repo__a-1", reward=0.0, n_agent_steps=5, exit_status="Submitted",
        trial_log=log, cert_dir=None,
    )
    assert rec["gt_prebuilt_active"] is False
    assert rec["failure_class"] == "GT"
    assert rec["in_resolved_denominator"] is True


def test_build_signal_record_agent_with_passing_certs(tmp_path):
    # Valid certs + prebuilt active + hash match + reward 0 + steps>0 -> AGENT.
    cert_dir = tmp_path / "gt"
    cert_dir.mkdir()
    (cert_dir / "graph_certificate.json").write_text(
        '{"verdict": "GRAPH_VALID", "pass": true}', encoding="utf-8")
    (cert_dir / "lsp_certificate.json").write_text(
        '{"verdict": "LSP_ACTIVE_VALID", "pass": true}', encoding="utf-8")
    (cert_dir / "embedder_certificate.json").write_text(
        '{"verdict": "EMBEDDER_USAGE_VALID", "pass": true}', encoding="utf-8")
    log = ("[GT_META] graph_witness ... | gt_prebuilt_active=true; "
           "hook_graph_hash_matches_post_lsp=True; substrate_digest=sha256:deadbeef\n")
    rec = do.build_signal_record(
        instance_id="repo__b-2", reward=0.0, n_agent_steps=20, exit_status="Submitted",
        trial_log=log, cert_dir=str(cert_dir),
    )
    assert rec["gt_prebuilt_active"] is True
    assert rec["hook_hash_match"] is True
    assert rec["cert_fail"] is False
    assert rec["failure_class"] == "AGENT"


def test_build_signal_record_gt_from_failing_cert(tmp_path):
    # A FAIL-verdict cert -> GT even with prebuilt active + hash match.
    cert_dir = tmp_path / "gt"
    cert_dir.mkdir()
    (cert_dir / "embedder_certificate.json").write_text(
        '{"verdict": "EMBEDDER_FAIL_ZERO_MODEL", "pass": false}', encoding="utf-8")
    log = ("[GT_META] ... | gt_prebuilt_active=true; "
           "hook_graph_hash_matches_post_lsp=True\n")
    rec = do.build_signal_record(
        instance_id="repo__c-3", reward=0.0, n_agent_steps=20, exit_status="Submitted",
        trial_log=log, cert_dir=str(cert_dir),
    )
    assert rec["cert_fail"] is True
    assert rec["failure_class"] == "GT"


def test_build_signal_record_resolved(tmp_path):
    # reward 1.0 + sound context -> RESOLVED, counted in the denominator.
    cert_dir = tmp_path / "gt"
    cert_dir.mkdir()
    (cert_dir / "graph_certificate.json").write_text(
        '{"verdict": "GRAPH_VALID", "pass": true}', encoding="utf-8")
    log = ("[GT_META] ... | gt_prebuilt_active=true; "
           "hook_graph_hash_matches_post_lsp=True\n")
    rec = do.build_signal_record(
        instance_id="repo__d-4", reward=1.0, n_agent_steps=14, exit_status="Submitted",
        trial_log=log, cert_dir=str(cert_dir),
    )
    assert rec["failure_class"] == "RESOLVED"
    assert rec["in_resolved_denominator"] is True
