r"""SM-7 ALL-CYLINDERS gate — the FAST in-process CI guard for the gate RUNNER itself.

The heavy proof (34 members x their landed suites + the subprocess inversion + the determinism
double-run) lives in ``scripts/swebench/sm7_gate.py`` and is run on demand
(``python scripts/swebench/sm7_gate.py``). This module is the cheap, deterministic guard that the
RUNNER stays correct: it enumerates dynamically, maps every enabled member (no accidental DARK),
the native-grammar sweep is leak-clean, the -v parser survives pytest's variable padding (the bug
this gate was born with), and the PREFLIGHT-GAP check still catches the known fail-closed hole and
names the exact function to fix. NO pytest-subprocess, NO graph.db, NO ONNX — pure imports.

It NEVER edits a product file; a FAIL here means the RUNNER drifted, not that GT was patched.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_GATE_PATH = os.path.join(_REPO, "scripts", "swebench", "sm7_gate.py")


def _load_gate():
    spec = importlib.util.spec_from_file_location("sm7_gate", _GATE_PATH)
    assert spec and spec.loader, f"cannot load the gate runner at {_GATE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    return _load_gate()


def test_runner_exists_and_imports(gate):
    assert callable(gate.main)
    assert callable(gate.run_pytest)
    assert callable(gate.check2_preflight_gap)


def test_members_enumerated_dynamically_not_hardcoded(gate):
    """The gate's enumeration IS PROFILE_MEMBERS['2'] (which contains every Profile-1 member incl.
    the W10 native-FORM flags) — read live from rl_profile, never a frozen list."""
    from groundtruth.runtime import rl_profile as rp
    p2 = set(rp.PROFILE_MEMBERS["2"])
    assert set(rp.PROFILE_MEMBERS["1"]).issubset(p2)                 # Profile-2 ⊇ Profile-1
    w10_native = {"GT_CONTRACT_NATIVE", "GT_EVIDENCE_NATIVE", "GT_NUDGE_NATIVE",
                  "GT_BRIEF_NATIVE", "GT_INSEAM_METRICS"}
    assert w10_native.issubset(rp.PROFILE_MEMBERS["1"])              # W10 native flags live in P1
    # every enabled member has a covering-test mapping -> no member is silently DARK-UNPROVEN.
    unmapped = sorted(p2 - set(gate.MEMBER_EVIDENCE))
    assert unmapped == [], f"enabled members with NO covering-test mapping (would be DARK): {unmapped}"
    # and the map introduces no phantom member.
    phantom = sorted(set(gate.MEMBER_EVIDENCE) - p2)
    assert phantom == [], f"MEMBER_EVIDENCE maps non-members: {phantom}"


def test_every_mapped_nodeid_is_tree_prefixed(gate):
    """Every evidence node-id routes to a real tree (artifact_deepswe/tests or tests) and carries
    a ``file.py::node`` shape — the router depends on both."""
    ids = [nid for ids in gate.MEMBER_EVIDENCE.values() for nid in ids]
    ids += list(gate.DOSE_NODEIDS)
    ids += [nid for _n, _d, nids in gate.REBAKE_ITEMS for nid in nids]
    for nid in ids:
        assert nid.startswith(("artifact_deepswe/tests/", "tests/")), nid
        assert "::" in nid and nid.split("::")[0].endswith(".py"), nid


def test_native_grammar_sweep_is_leak_clean(gate):
    """CHECK 4 in-process: every native renderer, fed a tag+identity-laced fixture, emits ZERO
    <gt-*> and ZERO test identity; render_cochange_native correct-abstains ('')."""
    rows, leaks = gate._native_sweep()
    assert leaks == [], f"native renderer(s) leaked a tag or test identity: {leaks}"
    # the sweep is non-vacuous: >=1 renderer produced signal-bearing bytes, and the internal-only
    # cochange renderer correctly abstains.
    assert any(r["verdict"] == gate.PASS for r in rows)
    assert any(r["name"] == "render_cochange_native" and r["verdict"] == gate.ABSTAIN for r in rows)


def test_result_parser_survives_variable_padding(gate):
    """Regression guard for the bug this gate shipped with: pytest -v right-pads SHORT node names
    with MULTIPLE spaces before the [ NN%] column. The parser must read both the tight and the
    heavily-padded line (and normalize the Windows backslash separator)."""
    tight = r"tests/runtime/test_x.py::test_a_very_long_descriptive_name PASSED [ 50%]"
    padded = r"tests\test_y.py::test_flag_off_no_op PASSED            [  7%]"
    m1 = gate._RES_RE.match(tight.strip())
    m2 = gate._RES_RE.match(padded.strip())
    assert m1 and m1.group("st") == "PASSED"
    assert m2 and m2.group("st") == "PASSED"
    assert m2.group("nid").replace("\\", "/") == "tests/test_y.py::test_flag_off_no_op"
    # a FAILED / ERROR line is captured too (so a red is never read as MISSING).
    mf = gate._RES_RE.match("tests/test_z.py::test_b FAILED [100%]")
    assert mf and mf.group("st") == "FAILED"


def test_preflight_gap_is_caught_and_names_the_fix(gate):
    """CHECK 2 (updated 2026-07-13, gap CLOSED): rl_profile.preflight now routes an UNSET env
    through resolve_default_token (production default "2") and fail-closes on missing
    capabilities — the gate must PASS this check. A FAIL here means the fix regressed."""
    res = gate.check2_preflight_gap()
    assert res["verdict"] == gate.PASS, (
        "PREFLIGHT-GAP FAILS — the unset->production-default fail-closed routing in "
        "rl_profile.preflight regressed (see the SM-7 gate fix, 2026-07-13).")


def test_preflight_gap_probe_matches_live_rl_profile():
    """The FIX is real against the live resolver: unset routes through resolve_default_token to
    the production default and fail-closes exactly like an explicit profile=2; only an explicit
    off token is genuinely off."""
    from groundtruth.runtime import rl_profile as rp
    assert rp.resolve_default_token({}) == "2"
    assert rp.preflight({}, []) != []                                # fail-closed on the unset default
    assert rp.preflight({"GT_RL_PROFILE": "2"}, []) != []            # fail-closed when explicitly set
    assert rp.preflight({"GT_RL_PROFILE": "off"}, []) == []          # explicit off stays off


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
