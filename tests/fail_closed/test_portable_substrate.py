"""Stage 4.2 — portable GT substrate runtime tests.

Prove the GT proof runtime is benchmark-team runnable: ONE `gt-run-proof` command inside a pinned
image produces ALL artifacts from a mounted read-only repo — no per-task pip, no model download,
no host GT execution, no task-image mutation — and the OH wrapper consumes the artifacts read-only.
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
_GRP = os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py")
_spec = importlib.util.spec_from_file_location("gt_run_proof_t", _GRP)
grp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grp)
_WF = os.path.join(ROOT, ".github", "workflows", "swebench_300task.yml")
_DEEPSWE_WF = os.path.join(ROOT, ".github", "workflows", "deepswe_full.yml")
_PROOF_SWEEP_WF = os.path.join(ROOT, ".github", "workflows", "deepswe_proof_sweep.yml")
_WRAP = os.path.join(ROOT, "scripts", "swebench", "oh_gt_full_wrapper.py")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── artifact contract ────────────────────────────────────────────────────────

def test_contract_lists_all_required_artifacts():
    out = grp.expected_outputs("/gt_artifacts")
    for a in ("graph.db", "runtime_context.json", "lsp_certificate.json", "graph_certificate.json",
              "embedder_certificate.json", "foundational_gate_report.json", "brief.txt"):
        assert os.path.join("/gt_artifacts", a) in out, a


def test_brief_is_a_required_artifact():
    # P0.1-c: brief.txt is proof artifact #8 — the agent consumes it read-only; a missing
    # brief is GT_ARTIFACT_MISSING, never a host-fallback WARN.
    assert "brief.txt" in grp.REQUIRED_ARTIFACTS


def test_runtime_flags_record_forbid_prebuilt_graph():
    # P1-i: run_manifest.runtime_flags must record GT_FORBID_PREBUILT_GRAPH (provenance).
    assert "GT_FORBID_PREBUILT_GRAPH" in grp.PROOF_FLAG_KEYS


# ── P0.1-c: emit_brief fail-closed (empty/raising brief is a missing artifact) ──

def _brief_obj(text):
    class _B:
        brief_text = text
    return _B()


def _brief_obj_with_localization(text, *, sem_count=1, proof=None):
    class _B:
        brief_text = text
        effective_w_sem = 0.5
        semantic_signal_count = sem_count
        rendered_candidate_count = len(proof or [])
        k_sem_top = len(proof or [])
        sem_components = [float((r.get("components", {}) or {}).get("sem", 0.0) or 0.0) for r in (proof or [])]
        localization_proof = proof or []
    return _B()


def test_emit_brief_empty_is_fail_closed(tmp_path):
    ok, detail = grp.emit_brief(str(tmp_path), "fix the bug", "/work", "/g.db",
                                generator=lambda **kw: _brief_obj(""))
    assert ok is False
    assert "EMPTY" in detail
    assert not os.path.exists(os.path.join(str(tmp_path), "brief.txt"))


def test_emit_brief_exception_is_fail_closed_not_swallowed(tmp_path):
    def _boom(**kw):
        raise RuntimeError("embedder dead")
    ok, detail = grp.emit_brief(str(tmp_path), "fix the bug", "/work", "/g.db", generator=_boom)
    assert ok is False
    assert "RuntimeError" in detail and "embedder dead" in detail


def test_emit_brief_writes_nonempty_brief(tmp_path):
    ok, detail = grp.emit_brief(str(tmp_path), "fix the bug", "/work", "/g.db",
                                generator=lambda **kw: _brief_obj("EDIT-TARGET: src/x.py"))
    assert ok is True
    with open(os.path.join(str(tmp_path), "brief.txt"), encoding="utf-8") as f:
        assert f.read() == "EDIT-TARGET: src/x.py"


def test_emit_brief_live_localization_diagnostic_halts_before_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_FULL_POTENTIAL", "1")
    bad = _brief_obj_with_localization(
        "EDIT-TARGET: src/x.py",
        sem_count=0,
        proof=[{"path": "src/x.py", "components": {}}],
    )
    ok, detail = grp.emit_brief(str(tmp_path), "fix the bug", "/work", "/g.db",
                                generator=lambda **kw: bad)
    assert ok is False
    assert "live localization diagnostic HALT" in detail
    assert "ZERO_EVIDENCE_DELIVERED" in detail
    assert "SEMANTIC_SIGNAL_ZERO" in detail
    assert not os.path.exists(os.path.join(str(tmp_path), "brief.txt"))
    assert os.path.exists(os.path.join(str(tmp_path), "localization_diagnostic.json"))


def test_emit_brief_live_localization_diagnostic_allows_supported_brief(tmp_path):
    good = _brief_obj_with_localization(
        "EDIT-TARGET: src/x.py",
        sem_count=1,
        proof=[{"path": "src/x.py", "components": {"sem": 0.42, "lex": 0.2}, "entered_via": "semantic_seed"}],
    )
    ok, detail = grp.emit_brief(str(tmp_path), "fix the bug", "/work", "/g.db",
                                generator=lambda **kw: good)
    assert ok is True, detail
    assert os.path.exists(os.path.join(str(tmp_path), "brief.txt"))
    assert os.path.exists(os.path.join(str(tmp_path), "localization_diagnostic.json"))


# ── P1-e: polyglot per-language verdict AGGREGATION (no sibling masking) ──────

def test_aggregate_fail_no_warm_fails_under_require():
    # RED->GREEN: a launched-but-never-warm language is a FAILURE even when a sibling
    # language passed — the old loop's `lsp_ok = any rc==0` masked it.
    ok, failures = grp.aggregate_lsp_verdicts(
        {"python": "LSP_ACTIVE_VALID", "go": "LSP_FAIL_NO_WARM"},
        require_lsp=True, any_success=True)
    assert ok is False
    assert failures == ["go=LSP_FAIL_NO_WARM"]


def test_aggregate_install_missing_fails_under_require():
    ok, failures = grp.aggregate_lsp_verdicts(
        {"python": "LSP_ACTIVE_VALID", "rust": "LSP_INSTALL_MISSING"},
        require_lsp=True, any_success=True)
    assert ok is False and failures == ["rust=LSP_INSTALL_MISSING"]


def test_aggregate_resolve_error_fails_under_require():
    ok, failures = grp.aggregate_lsp_verdicts(
        {"typescript": "LSP_RESOLVE_ERROR(rc=1)"}, require_lsp=True, any_success=False)
    assert ok is False and failures == ["typescript=LSP_RESOLVE_ERROR(rc=1)"]


def test_aggregate_unsupported_and_valid_pass():
    # Genuinely-unknown languages stay an honest no-op; valid verdicts pass.
    ok, failures = grp.aggregate_lsp_verdicts(
        {"python": "LSP_ACTIVE_VALID", "ruby": "LSP_UNSUPPORTED_EXPLICIT",
         "go": "LSP_NO_OP_VALID_WITH_WARM_SERVER"},
        require_lsp=True, any_success=True)
    assert ok is True and failures == []


def test_aggregate_no_language_resolved_fails_under_require():
    ok, failures = grp.aggregate_lsp_verdicts({}, require_lsp=True, any_success=False)
    assert ok is False and failures == ["<none>=NO_LANGUAGE_RESOLVED"]


def test_aggregate_without_require_records_but_passes():
    ok, failures = grp.aggregate_lsp_verdicts(
        {"go": "LSP_FAIL_NO_WARM"}, require_lsp=False, any_success=False)
    assert ok is True and failures == ["go=LSP_FAIL_NO_WARM"]


def test_aggregate_incidental_language_missing_server_is_non_fatal():
    # B-INFRA RED->GREEN: the live tutanota (TS repo) case — a few Java files make the
    # detector see `java`, whose server (jdtls) this substrate does not ship, so java =
    # LSP_INSTALL_MISSING. Its PRIMARY language (typescript) resolved fine, so the task
    # must NOT INFRA-die: incidental java is RECORDED but non-fatal.
    ok, failures = grp.aggregate_lsp_verdicts(
        {"typescript": "LSP_ACTIVE_VALID", "java": "LSP_INSTALL_MISSING"},
        require_lsp=True, any_success=True, primary_langs={"typescript"})
    assert ok is True, "incidental (non-primary) missing server must not fail-close"
    assert failures == ["java=LSP_INSTALL_MISSING"], "incidental failure still recorded"


def test_aggregate_primary_language_missing_server_still_fatal():
    # The other half: if the task's OWN (primary) language server is missing, the substrate
    # genuinely can't serve it -> still fail-closed (no silent degrade of the real target).
    ok, failures = grp.aggregate_lsp_verdicts(
        {"typescript": "LSP_INSTALL_MISSING", "java": "LSP_INSTALL_MISSING"},
        require_lsp=True, any_success=False, primary_langs={"typescript"})
    assert ok is False and failures == ["typescript=LSP_INSTALL_MISSING"]


def test_aggregate_no_primary_info_keeps_strict_backcompat():
    # primary_langs=None -> original strict gate (every failure fatal); protects the
    # existing pure unit tests / callers that don't pass primary_langs.
    ok, failures = grp.aggregate_lsp_verdicts(
        {"python": "LSP_ACTIVE_VALID", "java": "LSP_INSTALL_MISSING"},
        require_lsp=True, any_success=True)
    assert ok is False and failures == ["java=LSP_INSTALL_MISSING"]


def test_aggregate_warm_warn_primary_reaches_agent():
    # FIX-A COMPLETION red->green (live INFRA death 2026-07-02, go-only `abs-module-cache-flags`):
    # a go-ONLY repo whose sole primary server WARMS (project_ready, warm_probe_ok) but converts
    # zero of a tiny residual (gopls has no module cache offline) emits LSP_WARN_ZERO_CONVERSION,
    # and the resolve subprocess exits nonzero so any_success=False. Per FIX-A the warm server is
    # LIVE and must reach the agent with its tree-sitter graph — it must NOT fail-close.
    ok, failures = grp.aggregate_lsp_verdicts(
        {"go": "LSP_WARN_ZERO_CONVERSION"},
        require_lsp=True, any_success=False, primary_langs={"go"})
    assert ok is True, "warm-but-unproductive PRIMARY server must reach the agent, not fail-close"
    # same, primary_langs=None (the pure back-compat path) — a warm WARN is still live
    ok_none, _ = grp.aggregate_lsp_verdicts(
        {"go": "LSP_WARN_ZERO_CONVERSION"}, require_lsp=True, any_success=False)
    assert ok_none is True
    # MUTATION GUARD: a NEVER-warm primary server with the same any_success=False MUST still fail.
    ok_bad, fbad = grp.aggregate_lsp_verdicts(
        {"go": "LSP_FAIL_NO_WARM"}, require_lsp=True, any_success=False, primary_langs={"go"})
    assert ok_bad is False and fbad == ["go=LSP_FAIL_NO_WARM"], "never-warm server is still a hard fail"
    # and an INCIDENTAL warm WARN must not rescue a FAILED primary (fatal returns first)
    ok_mix, _ = grp.aggregate_lsp_verdicts(
        {"typescript": "LSP_FAIL_NO_WARM", "go": "LSP_WARN_ZERO_CONVERSION"},
        require_lsp=True, any_success=False, primary_langs={"typescript"})
    assert ok_mix is False, "a warm incidental server must not mask a dead PRIMARY server"


def test_derive_primary_langs_declared_wins_else_dominant():
    # P0-2 (Fable 2026-07-02): the DECLARED task language is the ONLY primary when known — a
    # vendored-language graph dominant must NOT be scoped (aiomonitor: a python task with 2378
    # vendored JS nodes must scope to python, not javascript — else false-kill on a JS-server crash
    # AND fail-open on a real python resolve-error).
    assert grp._derive_primary_langs(["javascript"], "javascript", "python") == {"python"}
    # vendored java (jdtls omitted) is never the DECLARED language -> never a fatal INSTALL_MISSING
    assert grp._derive_primary_langs(["java"], "java", "python") == {"python"}
    # BUG-D fallback: no declared language -> the LSP-filtered dominant langs[0] (not the unfiltered
    # _detect_lang, which can default python) is the primary, so a warm-WARN go/rust primary is live.
    prim = grp._derive_primary_langs(["go"], "python", None)
    assert prim == {"go"}
    ok, _ = grp.aggregate_lsp_verdicts({"go": "LSP_WARN_ZERO_CONVERSION"},
                                       require_lsp=True, any_success=False, primary_langs=prim)
    assert ok is True, "warm-WARN go primary must be live once it is the scoped primary"
    # MUTATION GUARD: the OLD scope (python, from unfiltered _detect_lang) drops go -> false fail
    ok_bad, _ = grp.aggregate_lsp_verdicts({"go": "LSP_WARN_ZERO_CONVERSION"},
                                           require_lsp=True, any_success=False, primary_langs={"python"})
    assert ok_bad is False
    # empty langs + declared -> declared
    assert grp._derive_primary_langs([], None, "go") == {"go"}


def test_runtime_context_id_matches_cert_context_id():
    # BUG-F red->green (Fable-caught 2026-07-02): the runtime_context.json id must byte-match the
    # cert's context_id(). context_id() reads GT_SOURCE_ROOT/GT_GRAPH_DB from os.environ (set only
    # in the cert SUBPROCESS); the writer computes the id in-process from (work, graph) via the
    # SHARED pure helper. Prove they agree — and that the OLD env-less in-process call diverges.
    import hashlib, os as _os
    from groundtruth.runtime import proof as _proof
    rr = _proof.runtime_root()
    # pure helper == the documented sha256 formula
    assert _proof.context_id_from(rr, "/w", "/g", "/m") == \
        hashlib.sha256("|".join([rr, "/w", "/g", "/m"]).encode("utf-8")).hexdigest()[:16]
    saved = {k: _os.environ.get(k) for k in ("GT_SOURCE_ROOT", "GT_GRAPH_DB", "GT_MODELS_ROOT")}
    try:
        _os.environ.update({"GT_SOURCE_ROOT": "/w", "GT_GRAPH_DB": "/g", "GT_MODELS_ROOT": "/m"})
        # the cert (env-driven) and the writer (path-driven) produce one identical token
        assert _proof.context_id() == _proof.context_id_from(rr, "/w", "/g", "/m")
        # MUTATION GUARD: the OLD broken BUG-F path (env-less in-process context_id) DIVERGES
        _os.environ.pop("GT_SOURCE_ROOT"); _os.environ.pop("GT_GRAPH_DB")
        assert _proof.context_id() != _proof.context_id_from(rr, "/w", "/g", "/m"), \
            "env-less context_id must differ — this is exactly the bug BUG-F closes"
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_per_language_certs_persisted_no_overwrite():
    # Source contract: each language's resolve pass writes lsp_certificate_<lang>.json
    # (no FAIL cert overwritten) and the dominant cert is copied to the canonical path.
    src = _read(os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py"))
    assert 'lsp_certificate_{lg}.json' in src
    assert "shutil.copyfile(_canon_src, cert_lsp)" in src


def test_print_contract(capsys):
    assert grp.main(["--print-contract"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert "graph.db" in j["outputs"] and "embedder_certificate.json" in j["outputs"]
    assert "no per-task pip install" in j["guarantees"]
    assert "no model download" in j["guarantees"]
    assert j["entrypoint"] == "gt-run-proof"


# ── no per-task pip / no model download / no host execution ──────────────────

def test_validate_requires_proof_flags(monkeypatch):
    for f in ("GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER",
              "GT_FORCE_ONNX_EMBEDDER", "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK"):
        monkeypatch.delenv(f, raising=False)
    v = grp.validate_proof_env()
    assert any("GT_PROOF_MODE" in x for x in v)
    assert any("GT_CONTAINERIZED" in x for x in v)


def test_validate_requires_baked_deps_not_pip(monkeypatch):
    # all flags set but deps NOT baked (host runner) -> report 'not baked', never pip-install/download.
    for f in ("GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER",
              "GT_FORCE_ONNX_EMBEDDER", "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK"):
        monkeypatch.setenv(f, "1")
    monkeypatch.setenv("GT_MODELS_ROOT", "/nonexistent/models")
    v = grp.validate_proof_env()
    assert any("not baked" in x for x in v)


def test_main_fails_closed_on_host(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_CONTAINERIZED", raising=False)
    assert grp.main(["--source-root", str(tmp_path), "--out", str(tmp_path / "art")]) == 2


# ── workflow reduces to the portable substrate command ───────────────────────

def test_workflow_uses_portable_gt_run_proof():
    t = _read(_WF)
    assert "gt-run-proof" in t


def test_workflow_substrate_pinned_by_digest():
    t = _read(_WF)
    # final/proof mode must pin the substrate image by digest (a @sha256 or an explicit digest var)
    assert "@sha256:" in t or "GT_SUBSTRATE_DIGEST" in t


def test_workflow_no_per_task_pip_install():
    t = _read(_WF)
    assert "pip install -q onnxruntime tokenizers numpy pyright" not in t


def test_workflow_does_not_provision_rust_src_in_task_container():
    """Substrate/GHA boundary: workflow may extract task dep stores, but must not
    mutate the task image to install rust-src on the fly."""
    t = _read(_DEEPSWE_WF)
    assert "rustup component add rust-src" not in t


def test_deepswe_workflow_does_not_own_per_language_lsp_budget_policy():
    """Per-language readiness budgets belong to gt-run-proof, not workflow shell."""
    t = _read(_DEEPSWE_WF)
    assert 'GT_LSP_READY_BUDGET_S="$LSP_BUDGET"' not in t
    assert "GT_LSP_READY_BUDGET_S_OVERRIDE" in t


def test_proof_sweep_workflow_does_not_reencode_per_language_lsp_budget_policy():
    """The proof sweep must prove the runtime contract, not fork budget policy."""
    t = _read(_PROOF_SWEEP_WF)
    assert 'GT_LSP_READY_BUDGET_S="$LSP_BUDGET"' not in t
    assert 'case "$TASK_LANG"' not in t
    assert "LSP readiness budget owner: gt-run-proof" in t


def test_proof_lsp_ready_budget_matches_resolve_single_source_of_truth():
    """LSP4 (Fable) RED→GREEN: the proof runtime's per-language readiness budget MUST equal what
    ``groundtruth.resolve`` would independently pick from its per-server table — ONE table, not
    two contradictory ones. The old hardcoded go=30 / rust=45 / else=20 was EXPORTED as
    ``GT_LSP_READY_BUDGET_S``, which OVERRIDES resolve's rust-analyzer=180 / jdtls=180 / gopls=60
    / typescript-language-server=150 → rust/java/ts were under-provisioned to 45/20/20s and
    converted 0 edges (the exact failure resolve's table was tuned to prevent).

    Mutation: reverting lsp_ready_budget_seconds to a divergent hardcoded table (e.g. rust→45)
    makes rust proof=45 ≠ resolve=180 → RED.
    """
    import importlib.util

    from groundtruth.resolve import (
        _KNOWN_SERVERS,
        _READY_BUDGET_S_BY_SERVER,
        _READY_BUDGET_S_DEFAULT,
    )

    spec = importlib.util.spec_from_file_location(
        "gt_run_proof_lsp4", os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    for lang in ("go", "rust", "java", "typescript", "javascript", "python"):
        proof = m.lsp_ready_budget_seconds(lang, {})
        server_bin = os.path.basename(_KNOWN_SERVERS.get(lang, "") or "")
        resolve_would = int(
            round(float(_READY_BUDGET_S_BY_SERVER.get(server_bin, _READY_BUDGET_S_DEFAULT)))
        )
        assert proof == resolve_would, (
            f"{lang}: proof budget {proof}s contradicts resolve.py {resolve_would}s "
            f"(server={server_bin!r}) — two tables, not one"
        )
    # the slow indexers must get the long budget resolve.py tuned (value regression guard)
    assert m.lsp_ready_budget_seconds("rust", {}) == 180
    assert m.lsp_ready_budget_seconds("java", {}) == 180
    # override precedence preserved
    assert m.lsp_ready_budget_seconds("rust", {"GT_LSP_READY_BUDGET_S_OVERRIDE": "7"}) == 7


# ── OH wrapper consumes the artifacts (does not rebuild a divergent graph) ────

def test_wrapper_consumes_artifact_dir():
    w = _read(_WRAP)
    assert "GT_CERT_DIR" in w or "/gt_artifacts" in w


# ── Step 5: portable path primary + fallback forbidden ───────────────────────

def test_portable_path_primary_skips_in_task():
    t = _read(_WF)
    assert "GT_PORTABLE_SUBSTRATE=1" in t
    assert "in-task LSP+gates SKIPPED" in t
    assert "GT_PORTABLE_SUBSTRATE:-0" in t  # the redundant in-task gate steps guard on it


def test_fallback_failure_classes_present():
    t = _read(_WF)
    assert "GT_SUBSTRATE_DIGEST_MISSING" in t
    assert "PROOF_RUNTIME_FALLBACK_FORBIDDEN" in t
    assert "SUBSTRATE_MISSING_CERTS" in t


# ── separation of concerns / anti-cheat (helper never sees the evaluator) ────

def test_eval_leakage_env_forbidden(monkeypatch, tmp_path):
    for v in ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "TEST_PATCH"):
        monkeypatch.delenv(v, raising=False)
    assert grp.eval_leakage(str(tmp_path)) == []  # clean
    monkeypatch.setenv("FAIL_TO_PASS", '["t::x"]')
    assert any("FAIL_TO_PASS" in x for x in grp.eval_leakage(str(tmp_path)))


def test_eval_leakage_file_forbidden(monkeypatch, tmp_path):
    for v in ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "TEST_PATCH", "GOLD_FILES"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "test_patch.diff").write_text("--- a\n+++ b\n")
    leaks = grp.eval_leakage(str(tmp_path))
    assert any("test_patch" in x for x in leaks)


def test_eval_leakage_allows_real_repo_tests(monkeypatch, tmp_path):
    # the repo's OWN tests are legitimate — a tests/ dir or test_foo.py must NOT trip the guard.
    for v in ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "TEST_PATCH", "GOLD_FILES"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "test_widget.py").write_text("def test_x(): assert True\n")
    assert grp.eval_leakage(str(tmp_path)) == []


def test_contract_lists_separation_guarantee(capsys):
    grp.main(["--print-contract"])
    j = json.loads(capsys.readouterr().out)
    assert any("leakage" in g for g in j["guarantees"])


# ── LSP coverage: polyglot + demand scope (un-throttle the 500-cap) ──────────

def test_issue_terms_filters_stopwords():
    terms = grp._issue_terms("The connection pool should not timeout because of the error")
    assert "connection" in terms and "timeout" in terms
    assert "should" not in terms and "the" not in [t.lower() for t in terms]


def test_demand_scope_empty_issue_is_whole_repo():
    # no terms or no graph -> [] (=> whole-repo, default cap); never raises
    assert grp._demand_scope_files("/tmp/nonexistent.db", "") == []
    assert grp._demand_scope_files("/tmp/nonexistent.db", "fix the connection bug") == []


def test_detect_langs_graceful_and_lsp_only():
    assert grp._detect_langs("/tmp/nonexistent.db") == []  # graceful on missing db
    assert "python" in grp._LSP_LANGS and "typescript" in grp._LSP_LANGS


# ── Fix #5: eval-leak env match is WORD-BOUNDARY, not substring ───────────────

def test_gt_localization_gold_files_is_flagged_leak_safe():
    # STALE-TEST REPAIR (Fable adjudication 2026-07-02): `GT_LOCALIZATION_GOLD_FILES` is now
    # correctly FLAGGED as a leak. The allowlist (`_EVAL_LEAK_ENV_ALLOW`) was deliberately emptied
    # to frozenset() and the old gold-diagnostic feature this var fed was removed (no setter/reader
    # exists anywhere). Its VALUE is a gold-file list, so allowlisting would propagate gold paths
    # into the proof subprocess env (the surface that composes agent-visible artifacts), and gate
    # item 2 already treats its presence in trial_output.log as leakage. Leak-safe: flag it.
    assert grp._env_is_eval_leak("GT_LOCALIZATION_GOLD_FILES") is True


def test_env_leak_still_catches_real_leaks():
    # Bare + prefixed evaluator artifacts still trip (word-run match, not disabled).
    assert grp._env_is_eval_leak("GOLD_FILES") is True
    assert grp._env_is_eval_leak("SWE_GOLD_FILES") is True
    assert grp._env_is_eval_leak("FAIL_TO_PASS") is True
    assert grp._env_is_eval_leak("SWE_TEST_PATCH") is True


def test_env_leak_word_boundary_not_substring():
    # MUTATION-CHECK: a token embedded MID-WORD is NOT a leak (a substring match would
    # wrongly flag these; word-boundary must not). Reverting to `tok in ku` fails here.
    assert grp._env_is_eval_leak("LATEST_PATCHSET") is False   # substring has "TEST_PATCH"
    assert grp._env_is_eval_leak("PYTHONPATH") is False


def test_eval_leakage_gt_localization_var_is_flagged(monkeypatch, tmp_path):
    # STALE-TEST REPAIR (Fable adjudication 2026-07-02): the var's value is a gold-file list, so
    # eval_leakage MUST flag it (leak-safe) — the empty allowlist is deliberate hardening.
    for v in ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "TEST_PATCH", "GOLD_FILES"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GT_LOCALIZATION_GOLD_FILES", "src/a.py,src/b.py")
    assert "env:GT_LOCALIZATION_GOLD_FILES" in grp.eval_leakage(str(tmp_path))
    # and a bare real leak alongside it IS also caught
    monkeypatch.setenv("GOLD_FILES", "secret")
    assert any(x == "env:GOLD_FILES" for x in grp.eval_leakage(str(tmp_path)))


# ── Fix #6: the Go workspace-metadata probe is OFFLINE by default ─────────────

class _FakeCP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_go_probe_defaults_goproxy_offline(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _FakeCP(rc=0, out="pkgA\npkgB\n")

    monkeypatch.setattr(grp.subprocess, "run", _fake_run)
    monkeypatch.delenv("GT_PROOF_ALLOW_NETWORK", raising=False)
    r = grp.probe_workspace_metadata("go", str(tmp_path), {"PATH": "/x"})
    assert captured["env"]["GOPROXY"] == "off", "sealed proof probe must not reach the network"
    assert captured["env"]["GOFLAGS"] == "-mod=mod"
    assert r["status"] == "ok"


def test_go_probe_network_only_when_explicitly_allowed(monkeypatch, tmp_path):
    captured = {}

    def _fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return _FakeCP(rc=0, out="")

    monkeypatch.setattr(grp.subprocess, "run", _fake_run)
    monkeypatch.setenv("GT_PROOF_ALLOW_NETWORK", "1")
    grp.probe_workspace_metadata("go", str(tmp_path), {"PATH": "/x"})
    assert "proxy.golang.org" in captured["env"]["GOPROXY"]


# ── Fix #1: STRICT (GT_GATES_DELIVER_ALWAYS=0) fails-closed on genuinely-broken
#            signals ONLY — the name_match-dominance carve-out is preserved ─────

def _write_report(tmp_path, *, g1=True, g2=True, embedder_present=True, stamp="", granular=True):
    rep = {
        "verdict": {"resolution_jarvis": g1, "lsp_enrichment": g2,
                    "embedder": True, "all_on": g1 and g2},
        "gate_lsp": {"stamp_mismatch": stamp},
        "gate_embedder": {"present": {"pass": embedder_present}},
    }
    if granular:
        # STALE-FIXTURE REPAIR (Fable adjudication 2026-07-02): the LIVE report carries the granular
        # gate_resolution block (foundational_gates emits pred_A_det_floor on every readable graph).
        # _strict_broken_gate_signals reads the granular pred_A, NOT the combined resolution_jarvis —
        # benign name_match DOMINANCE = pred_A above the SAFETY floor (True) + pred_B non-dominance
        # False. Omitting this block is the PRE-granularity shape, which the code deliberately
        # fail-closes (that shape is also produced by graph.db-missing / 0-CALLS-edges).
        rep["gate_resolution"] = {"pred_A_det_floor": True, "pred_B_nondominance": False}
    p = tmp_path / "foundational_gate_report.json"
    p.write_text(json.dumps(rep), encoding="utf-8")
    return str(p)


def test_strict_all_green_is_deliverable(tmp_path):
    assert grp._strict_broken_gate_signals(_write_report(tmp_path)) == []


def test_strict_name_match_dominance_g1_off_is_carved_out(tmp_path):
    # MUTATION-CHECK: GATE 1 off (name_match-heavy JS/TS/Go map) with the GRANULAR pred_A above the
    # SAFETY floor is EXPECTED and must NOT be a broken signal (matches the live clack artifact:
    # pred_A_det_floor True, pred_B_nondominance False -> deliverable carve-out).
    assert grp._strict_broken_gate_signals(_write_report(tmp_path, g1=False)) == []


def test_strict_g1_off_without_granularity_is_conservative(tmp_path):
    # Fable adjudication companion: the PRE-granularity shape (no gate_resolution) is also produced
    # by the genuinely-catastrophic early returns (graph.db missing / 0 CALLS edges). With jarvis
    # False and pred_A unavailable, STRICT must fail-close conservatively — never carve out a dead graph.
    r = grp._strict_broken_gate_signals(_write_report(tmp_path, g1=False, granular=False))
    assert r and any("pred_A unavailable" in s for s in r), f"expected conservative fail-close, got {r}"


def test_strict_stamp_drop_is_broken(tmp_path):
    broken = grp._strict_broken_gate_signals(
        _write_report(tmp_path, stamp="LSP_STAMP_DROPPED_AFTER_RESOLVE"))
    assert any("LSP_STAMP_DROPPED_AFTER_RESOLVE" in b for b in broken)


def test_strict_lsp_dark_is_broken(tmp_path):
    broken = grp._strict_broken_gate_signals(_write_report(tmp_path, g2=False))
    assert any("LSP_DARK" in b for b in broken)


def test_strict_dead_embedder_is_broken(tmp_path):
    broken = grp._strict_broken_gate_signals(_write_report(tmp_path, embedder_present=False))
    assert any("EMBEDDER_DEAD" in b for b in broken)


def test_strict_unreadable_report_returns_none(tmp_path):
    assert grp._strict_broken_gate_signals(str(tmp_path / "missing.json")) is None


# ── Fix #2: GT_REQUIRE_LSP aggregate failure is FAIL-CLOSED (not WARN-continue) ─

def test_require_lsp_aggregate_failure_is_fail_closed():
    src = _read(_GRP)
    assert "LSP_LIVENESS_FAIL" in src           # the fail-closed path exists
    assert "LSP_LIVENESS_WARN" not in src        # the old warn-and-continue is gone
    # the fail-closed return is wired to the aggregate check
    assert 'tracker.fail(\n            "lsp_pass",\n            "LSP_LIVENESS_FAIL"' in src
