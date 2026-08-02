r"""Fable-LIPI round-2 (2026-07-11): the PROSE leak predicate is SINGLE-SOURCED across the two
model-facing prose surfaces (the brief's ``_obligation_is_leaky`` and the seam's
``_prose_leaks_test_identity``) via ``native_render.prose_leaks_test_identity``, so they cannot
drift. Round-1 tuned each surface to its own examples and each missed a DIFFERENT half:

  * the brief missed ``assert_eq!`` (rust macro), ``utils.test.ts`` (JS/TS file), ``crate::tests::``;
  * the seam had NO assertion leg at all (``assertEqual`` sailed through);
  * BOTH missed the case+underscore variants ``Test_Reconnect`` / ``TEST_LOGIN``.

These were CONFIRMED reaching model-facing bytes (scratchpad probe_a/probe_b). This test PINS the
closed set: every leak class flagged, every production near-miss kept, and all three predicates in
lockstep. A mutation to any canonical leg (e.g. dropping the ``(?i:)`` on the snake leg, or the
``_\w+`` tail on the assert leg) makes the corresponding row bite.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "artifact_deepswe"))

from groundtruth.runtime import native_render as _nr  # noqa: E402
from groundtruth.pretask.v1r_brief import _obligation_is_leaky  # noqa: E402
import gt_mini_patch as _seam  # noqa: E402


def _brief(s: str) -> bool:
    # name/assert legs only (no F2P marker, no gold-path token) so the brief screen == canonical.
    return bool(_obligation_is_leaky(s, set(), set()))


def _canonical(s: str) -> bool:
    return bool(_nr.prose_leaks_test_identity(s))


def _seam_screen(s: str) -> bool:
    return bool(_seam._prose_leaks_test_identity(s))


# Each entry is prose an issue could carry; the token is a real test/assertion identity that must
# NOT reach the model. (No nodeid/path forms here — those are also caught, but these isolate the
# BARE-name + assertion coverage that round-1 missed.)
LEAKS = [
    ("pytest-snake", "the fix must make test_login_flow pass again"),
    ("go-pascal", "TestReconnect should succeed after the retry change"),
    ("go-underscore-cap", "Test_Reconnect fails after the timeout"),
    ("go-underscore-lower", "Test_reconnect fails after the timeout"),
    ("upper-snake", "TEST_LOGIN fails on retry"),
    ("suffix-test", "widget_test broke in CI"),
    ("camelCase", "testShouldReconnect currently fails"),
    ("rust-assert-macro", "the parser must satisfy assert_eq!(parse(''), Err) on empty input"),
    ("rust-assert-ne", "assert_ne!(a, b) is expected to hold"),
    ("unittest-assert", "the client must assertEqual(None, q.pop()) when empty"),
    ("jsts-test-file", "the suite utils.test.ts should pass once the guard lands"),
    ("jsts-spec-file", "foo.spec.js is red until the fix lands"),
    ("rust-mod-path", "crate::tests::reconnect panics on empty input"),
    ("rust-test-attr", "#[test] fn reconnect_works panics on empty input"),
    ("mocha-it", "it('rejects bad input') fails in CI"),
    ("pytest-nodeid", "failing at tests/net/conn_flow.py::TestConnFlow succeeds"),
    ("test-path-seg", "see tests/unit/widget_helpers.py for the fixture"),
    # Fable-LIPI round-2 seam Finding-1 (2026-07-11): NON-`tests/` test-DIR file paths. The
    # canonical regex dir leg is only `\btests?[\\/]`; these leak unless the path belt
    # (native_render.contains_test_identity -> path_policy.is_test_path, the full segment set
    # {spec,specs,__tests__,e2e,...}) is folded into the prose screen. Basenames are plain
    # (helpers.js / router.js), so ONLY the directory segment triggers — isolates the belt.
    ("jest-spec-dir", "the regression lives in spec/helpers.js per the failing trace"),
    ("jest-tests-dir", "the router bug is in __tests__/router.js and must be fixed"),
    ("rspec-specs-dir", "reproduce it from specs/writer_check.js before patching"),
    ("e2e-dir", "the flow breaks at e2e/checkout.js on submit"),
]

# Production identifiers an issue legitimately carries — must SURVIVE (leak=0 cuts both ways: an
# over-drop guts a real obligation). These are the near-miss keep-list the fix is boundary-guarded
# against.
KEEPS = [
    ("prod-TestingConfig", "TestingConfig is loaded at startup"),
    ("prod-contest", "the contest_handler runs on submit"),
    ("prod-latest", "the latest_value is cached per request"),
    ("prod-rust-scope", "std::collections::HashMap is the backing store"),
    ("prod-self-scope", "call self::module::func to resolve"),
    ("prod-shouldRetry", "set shouldRetry to true on a dropped connection"),
    ("prod-attest", "attestation succeeds when the nonce matches"),
    ("prod-manifest", "manifest.json is written to the build dir"),
    ("prod-plain", "the parser must coalesce adjacent tokens after the change"),
    # Fable-LIPI round-2 brief Finding-2 (2026-07-11): the bare RFC-2119/EARS verb "assert" is
    # a legit obligation, NOT a test identity. Round-1's optional-tail `\bassert(?:...)?\b` ate
    # it whole; the narrowed `\bassert(?:_\w+|[A-Z]\w*|!|\s*\()` releases the verb while still
    # catching assertEqual / assert_eq! / assert(. These MUST survive.
    ("prod-assert-verb", "the client must assert that the payload is non-null"),
    ("prod-assert-verb2", "the code should assert the invariant holds after the write"),
]


@pytest.mark.parametrize("name,text", LEAKS)
def test_leak_flagged_by_all_three_surfaces(name, text):
    assert _canonical(text), f"canonical missed {name!r}: {text!r}"
    assert _brief(text), f"brief missed {name!r}: {text!r}"
    assert _seam_screen(text), f"seam missed {name!r}: {text!r}"


@pytest.mark.parametrize("name,text", KEEPS)
def test_near_miss_kept_by_all_three_surfaces(name, text):
    assert not _canonical(text), f"canonical OVER-DROPPED {name!r}: {text!r}"
    assert not _brief(text), f"brief OVER-DROPPED {name!r}: {text!r}"
    assert not _seam_screen(text), f"seam OVER-DROPPED {name!r}: {text!r}"


def test_brief_and_seam_agree_with_canonical_on_whole_corpus():
    # The single-source invariant: no surface may leak a class another catches, and none may
    # over-drop a keep another keeps. If the brief/seam mirror drifts from the canonical, this bites.
    for name, text in (LEAKS + KEEPS):
        c, b, s = _canonical(text), _brief(text), _seam_screen(text)
        assert b == c, f"BRIEF drifted from canonical on {name!r}: brief={b} canonical={c}"
        assert s == c, f"SEAM drifted from canonical on {name!r}: seam={s} canonical={c}"


def test_seam_local_mirror_matches_canonical():
    # The seam keeps a LOCAL mirror of the canonical patterns purely as the fail-open fallback for
    # a broken in-container import. Pin it byte-equal to native_render so a one-sided edit bites.
    assert _seam._PROSE_TEST_NAME_RE.pattern == _nr.PROSE_TEST_NAME_RE.pattern
    assert _seam._PROSE_ASSERT_RE.pattern == _nr.PROSE_ASSERT_RE.pattern


@pytest.mark.xfail(
    reason="DELIBERATELY DECLINED: bare JUnit-style `shouldReturnX` collides with production "
    "camelCase booleans (`shouldRetry`) and spaced behavioral prose; catching it over-drops real "
    "obligations. Gate a length/segment discriminator on real DeepSWE issue data, not a guess.",
    strict=True,
)
def test_junit_should_style_is_a_known_declined_residual():
    assert _canonical("shouldReturnEmptyWhenNoUsers fails in CI")


@pytest.mark.xfail(
    reason="DELIBERATELY DECLINED (Fable-LIPI round-2 brief Finding-2, 2026-07-11): production "
    "identifiers shape-identical to test names — the `test_mode` flag, Starlette `TestClient`, "
    "unittest `TestCase` base, statsmodels `adf_test` — are indistinguishable from a real test "
    "name (`test_login_flow`/`TestReconnect`) by regex alone. correct-or-quiet fails CLOSED "
    "(drop the obligation) rather than risk leaking the grader target; the precision cost is a "
    "documented residual. The GRAPH surfaces avoid this via is_test+path gates, but the PROSE "
    "screen has no such signal. Any release must be gated on real DeepSWE issue data, not a guess.",
    strict=True,
)
@pytest.mark.parametrize("text", [
    "the test_mode flag must default to false",
    "TestClient must forward the Set-Cookie header",
    "subclasses of TestCase must call super().setUp()",
])
def test_prod_name_collision_is_a_known_declined_residual(text):
    # These SHOULD survive as legit obligations but are dropped (fail-closed) — pinned xfail so
    # the residual is a visible tripwire, not a silent precision gap.
    assert not _canonical(text)
