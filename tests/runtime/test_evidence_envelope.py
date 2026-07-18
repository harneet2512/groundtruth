"""EvidenceEnvelope contract — Stage-1 deterministic acceptance (TTD).

Every test names the LAW it pins. The envelope is the early-fixed internal protocol
(gt_gt §24-§26 / GT_LAYERS_SOURCE §B): pure, deterministic, LLM-free, no time / no
randomness / no I/O. These tests assert the four laws, the enum sanity floor, the
JSON round-trip exactness, and dedup determinism — plus the two mutation targets the
build proves bite (the leak predicate; the unmeasured=>advisory gate).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from groundtruth.runtime import evidence_envelope as envelope_module
from groundtruth.runtime.evidence_envelope import (
    ADVISORY,
    BLOCKING,
    EVENT_EDIT,
    HYPOTHESIS,
    INFO,
    ONE_BOUNCE,
    RECEIPT_ACTED,
    RECEIPT_CAUSAL,
    RECEIPT_DELIVERED,
    RECEIPT_NONE,
    RECEIPT_REFERENCED,
    RECEIPT_RESOLVED_STATE,
    VERIFIED,
    WARNING,
    EvidenceEnvelope,
    chain_hash,
    content_signature,
    derive_dedup_key,
    from_dict,
    render_bytes,
    to_dict,
    validate,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(**over: object) -> EvidenceEnvelope:
    """A law-abiding envelope; keyword overrides mutate one dimension per test."""
    base = dict(
        producer="def_ref_partition",
        fact_id="F1",
        target="src/users.py",
        evidence_type="def_ref_partition",
        payload=("def: src/users.py:120",),
        provenance=(("src/users.py", 120),),
        confidence=0.9,
        tier=VERIFIED,
        graph_revision="abc123",
        preferred_event=EVENT_EDIT,
        blocking_eligibility=ADVISORY,
        estimated_cost_tokens=12,
        measured=False,
    )
    base.update(over)
    return EvidenceEnvelope.build(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# baseline
# --------------------------------------------------------------------------- #
def test_clean_envelope_has_no_violations() -> None:
    assert validate(_clean()) == []


def test_frozen_dataclass_is_immutable() -> None:
    env = _clean()
    with pytest.raises(Exception):  # FrozenInstanceError is a dataclasses subclass
        env.target = "other.py"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# LAW (a) — LEAK LAW: target + every provenance path must pass path_policy
# --------------------------------------------------------------------------- #
def test_leak_law_flags_test_target() -> None:
    v = validate(_clean(target="tests/test_users.py"))
    assert any("leak" in s and "target" in s for s in v)


def test_leak_law_flags_test_provenance_row() -> None:
    v = validate(_clean(provenance=(("tests/test_users.py", 9),)))
    assert any("leak" in s and "provenance" in s for s in v)


def test_leak_law_flags_vendored_target() -> None:
    v = validate(_clean(target="node_modules/lib/index.js"))
    assert any("leak" in s for s in v)


def test_leak_law_flags_demo_docs_path() -> None:
    v = validate(_clean(target="docs_src/tutorial/app.py"))
    assert any("leak" in s for s in v)


def test_leak_law_allows_bare_symbol_target() -> None:
    # a bare symbol has no path shape -> path_policy passes it -> no leak violation.
    assert not any("leak" in s for s in validate(_clean(target="get_user")))


# --------------------------------------------------------------------------- #
# LAW (b) — TIER HONESTY
# --------------------------------------------------------------------------- #
def test_verified_requires_nonempty_provenance() -> None:
    v = validate(_clean(tier=VERIFIED, provenance=(), confidence=0.9))
    assert any("VERIFIED requires nonempty provenance" in s for s in v)


def test_verified_requires_confidence_floor() -> None:
    v = validate(_clean(tier=VERIFIED, confidence=0.5))
    assert any("VERIFIED requires confidence" in s for s in v)


def test_hypothesis_caps_confidence_below_floor() -> None:
    v = validate(_clean(tier=HYPOTHESIS, confidence=0.9, provenance=()))
    assert any("HYPOTHESIS caps confidence" in s for s in v)


def test_hypothesis_below_floor_is_clean() -> None:
    assert validate(_clean(tier=HYPOTHESIS, confidence=0.4, provenance=())) == []


def test_warning_info_have_no_tier_conf_law() -> None:
    # WARNING/INFO carry no VERIFIED/HYPOTHESIS confidence constraint.
    assert validate(_clean(tier=WARNING, confidence=0.55, provenance=())) == []
    assert validate(_clean(tier=INFO, confidence=0.1, provenance=())) == []


def test_confidence_out_of_bounds_flagged() -> None:
    assert any("confidence" in s for s in validate(_clean(confidence=1.5)))
    assert any("confidence" in s for s in validate(_clean(confidence=-0.1, tier=INFO,
                                                          provenance=())))


# --------------------------------------------------------------------------- #
# LAW (c) — UNMEASURED => ADVISORY (nothing unmeasured can be enforcing)
# --------------------------------------------------------------------------- #
def test_unmeasured_blocking_is_a_violation() -> None:
    v = validate(_clean(blocking_eligibility=BLOCKING, measured=False))
    assert any("requires measured=True" in s for s in v)


def test_unmeasured_one_bounce_is_a_violation() -> None:
    v = validate(_clean(blocking_eligibility=ONE_BOUNCE, measured=False))
    assert any("requires measured=True" in s for s in v)


def test_measured_blocking_is_allowed() -> None:
    assert not any("measured" in s for s in
                   validate(_clean(blocking_eligibility=BLOCKING, measured=True)))


def test_advisory_never_needs_measured() -> None:
    assert not any("measured" in s for s in
                   validate(_clean(blocking_eligibility=ADVISORY, measured=False)))


# --------------------------------------------------------------------------- #
# LAW (d) — DEDUP DETERMINISM (5-component derivation: the F1 lesson flows IN).
# The key is hash(producer, evidence_type, target, fact_id, content) where
# fact_id is the producer-natural SYMBOL and content is the SHIPPED
# payload+provenance serialization — so two DISTINCT facts sharing a target
# never collide, and the same fact re-fires when its content changes.
# --------------------------------------------------------------------------- #
def test_dedup_key_matches_derivation() -> None:
    env = _clean()
    assert env.dedup_key == derive_dedup_key(
        env.producer, env.evidence_type, env.target, env.fact_id,
        content_signature(env.payload, env.provenance))
    assert validate(env) == []


def test_dedup_key_mismatch_is_a_violation() -> None:
    from dataclasses import replace

    tampered = replace(_clean(), dedup_key="deadbeefdeadbeef")
    assert any("dedup_key" in s for s in validate(tampered))


def test_derive_dedup_key_is_stable_no_randomness() -> None:
    a = derive_dedup_key("p", "k", "t")
    b = derive_dedup_key("p", "k", "t")
    assert a == b and len(a) == 16
    # different inputs -> different key (fact identity separates)
    assert derive_dedup_key("p", "k", "t") != derive_dedup_key("p", "k2", "t")


def test_derive_dedup_key_fact_id_and_content_separate_identities() -> None:
    """The F1 discipline at derivation level: the symbol (fact_id) and the shipped
    content are BOTH identity components — omitting either re-opens the collision."""
    base = derive_dedup_key("p", "k", "t", "sym1", "c1")
    assert base == derive_dedup_key("p", "k", "t", "sym1", "c1")   # stable
    assert base != derive_dedup_key("p", "k", "t", "sym2", "c1")   # symbol separates
    assert base != derive_dedup_key("p", "k", "t", "sym1", "c2")   # content separates


def test_f1_collision_two_symbols_same_target_distinct_keys() -> None:
    """ENVELOPE-LEVEL F1 pin: two DIFFERENT symbols (run/handle) whose def sets
    share the same alphabetically-first file (== same target) must carry DISTINCT
    dedup keys — the exact gateway collision fixed in the bounce round."""
    run = _clean(
        fact_id="run", target="core/base.py",
        payload=("def: core/base.py:10", "def: m_other.py:20"),
        provenance=(("core/base.py", 10), ("m_other.py", 20)),
    )
    handle = _clean(
        fact_id="handle", target="core/base.py",
        payload=("def: core/base.py:30", "def: n_other.py:40"),
        provenance=(("core/base.py", 30), ("n_other.py", 40)),
    )
    assert run.dedup_key != handle.dedup_key
    assert validate(run) == [] and validate(handle) == []


def test_f1_freshness_changed_content_changes_key() -> None:
    """Same symbol, same target, CHANGED def-set (an edit moved the def) -> a new
    key, so the kernel's suppression chain does not wrongly latch a stale fact."""
    before = _clean(fact_id="run", payload=("def: src/users.py:120",),
                    provenance=(("src/users.py", 120),))
    after = _clean(fact_id="run", payload=("def: src/users.py:499",),
                   provenance=(("src/users.py", 499),))
    assert before.dedup_key != after.dedup_key


# --------------------------------------------------------------------------- #
# enum sanity floor
# --------------------------------------------------------------------------- #
def test_unknown_tier_flagged() -> None:
    assert any("tier" in s for s in validate(_clean(tier="BOGUS", provenance=())))


def test_unknown_blocking_level_flagged() -> None:
    assert any("blocking_eligibility" in s for s in
               validate(_clean(blocking_eligibility="hard")))


def test_unknown_preferred_event_flagged() -> None:
    assert any("preferred_event" in s for s in
               validate(_clean(preferred_event="startup")))


# --------------------------------------------------------------------------- #
# to_dict / from_dict round-trip + deterministic ordering
# --------------------------------------------------------------------------- #
def test_roundtrip_exact_equality() -> None:
    env = _clean()
    assert from_dict(to_dict(env)) == env


def test_roundtrip_through_json_exact_equality() -> None:
    env = _clean(blocking_eligibility=BLOCKING, measured=True)
    restored = from_dict(json.loads(json.dumps(to_dict(env))))
    assert restored == env


def test_observation_binding_roundtrips_without_changing_rendered_bytes() -> None:
    build = getattr(envelope_module, "build_observation_binding", None)
    assert callable(build), "Cluster 1 requires one reusable observation-binding authority"
    binding = build(
        batch_start_iteration=3,
        parent_policy_sha256=hashlib.sha256(b"parent").hexdigest(),
        parent_policy_chars=6,
        action_batch_sha256=hashlib.sha256(b"actions").hexdigest(),
        candidate_ordinal=1,
        candidate_kind="gateway.def_ref_partition",
        candidate_id=_clean().dedup_key,
    )
    env = _clean(observation_binding=binding)

    assert render_bytes(env) == render_bytes(_clean())
    restored = from_dict(json.loads(json.dumps(to_dict(env))))
    assert restored.observation_binding == binding
    assert validate(restored) == []


def test_observation_binding_rejects_mismatched_candidate_identity() -> None:
    build = getattr(envelope_module, "build_observation_binding", None)
    assert callable(build), "Cluster 1 requires one reusable observation-binding authority"
    binding = build(
        batch_start_iteration=0,
        parent_policy_sha256=hashlib.sha256(b"parent").hexdigest(),
        parent_policy_chars=6,
        action_batch_sha256=hashlib.sha256(b"actions").hexdigest(),
        candidate_ordinal=0,
        candidate_kind="gateway.def_ref_partition",
        candidate_id="different-candidate",
    )
    env = _clean(observation_binding=binding)

    assert any(
        "observation_binding" in issue and "candidate_id" in issue
        for issue in validate(env)
    )


def test_to_dict_key_order_is_deterministic() -> None:
    env = _clean()
    keys = list(to_dict(env).keys())
    assert keys == list(EvidenceEnvelope.FIELD_ORDER)
    # two serializations of the same envelope are byte-identical (no set/time nondeterminism)
    assert json.dumps(to_dict(env)) == json.dumps(to_dict(_clean()))


def test_build_defaults_valid_until_to_graph_revision() -> None:
    env = _clean(graph_revision="rev9")
    assert env.valid_until == "rev9"  # freshness token defaults to the graph revision


def test_build_derives_dedup_key() -> None:
    env = _clean()
    assert env.dedup_key == derive_dedup_key(
        "def_ref_partition", "def_ref_partition", "src/users.py", "F1",
        content_signature(("def: src/users.py:120",), (("src/users.py", 120),)))


# --------------------------------------------------------------------------- #
# multi-violation accumulation (validate returns ALL, not first)
# --------------------------------------------------------------------------- #
def test_validate_accumulates_multiple_violations() -> None:
    from dataclasses import replace

    bad = replace(
        _clean(tier=VERIFIED, provenance=(), confidence=0.5,
               target="tests/test_x.py", blocking_eligibility=BLOCKING, measured=False),
        dedup_key="wrong",
    )
    v = validate(bad)
    # leak + VERIFIED-provenance + VERIFIED-conf + unmeasured-block + dedup mismatch
    assert len(v) >= 5


# =========================================================================== #
# TITO CONTRACT — the RL-training-safe delivery metadata + conformance laws.
# The 10-law split (module docstring): locally-testable-now = 1 (append-only),
# 5 (observation-prefix hash-chain), 9 (leak), 10 (byte-determinism); adapter
# obligations = 2, 8 (W2); trainer-integration obligations = 3, 4, 6, 7.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# DELIVERABLE 1 — new metadata fields with safe defaults + exact round-trip.
# --------------------------------------------------------------------------- #
def test_new_tito_fields_default_safely() -> None:
    env = _clean()
    assert env.episode_id == ""
    assert env.event_id == ""
    assert env.parent_observation_hash == ""
    assert env.payload_schema_version == "env.1"
    assert env.renderer_id == ""
    assert env.rendered_bytes_hash == ""
    assert env.receipt_state == RECEIPT_NONE == "none"
    assert env.delivery_reason == ""
    assert env.suppression_reason == ""
    # defaults keep a built envelope law-abiding.
    assert validate(env) == []


def test_roundtrip_exact_with_tito_fields() -> None:
    from dataclasses import replace

    env = replace(
        _clean(),
        episode_id="ep-7",
        event_id="ev-3",
        parent_observation_hash="ab12cd",
        payload_schema_version="env.1",
        renderer_id="native_v1",
        rendered_bytes_hash="deadbeef",
        receipt_state=RECEIPT_DELIVERED,
        delivery_reason="fired_at_decision_point",
    )
    assert from_dict(to_dict(env)) == env
    assert from_dict(json.loads(json.dumps(to_dict(env)))) == env


def test_to_dict_key_order_includes_tito_fields() -> None:
    keys = list(to_dict(_clean()).keys())
    assert keys == list(EvidenceEnvelope.FIELD_ORDER)
    for f in (
        "episode_id", "event_id", "parent_observation_hash", "payload_schema_version",
        "renderer_id", "rendered_bytes_hash", "receipt_state", "delivery_reason",
        "suppression_reason",
    ):
        assert f in keys


# --------------------------------------------------------------------------- #
# DELIVERABLE 2 (e) — receipt_state requires delivery evidence + legal value.
# --------------------------------------------------------------------------- #
def test_receipt_state_requires_rendered_bytes_hash() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), receipt_state=RECEIPT_DELIVERED, rendered_bytes_hash=""))
    assert any("receipt_state" in s and "rendered_bytes_hash" in s for s in v)


def test_receipt_state_with_hash_is_clean() -> None:
    # F1 adjustment: the delivery-evidence hash is a REAL sha256 hex digest (64 chars)
    # — law (f) now pins width, so the old 10-hex stand-in is itself a violation.
    from dataclasses import replace

    h = hashlib.sha256(b"rendered").hexdigest()
    for st in (RECEIPT_DELIVERED, RECEIPT_REFERENCED, RECEIPT_ACTED,
               RECEIPT_RESOLVED_STATE, RECEIPT_CAUSAL):
        env = replace(_clean(), receipt_state=st, rendered_bytes_hash=h)
        assert not any("receipt_state" in s for s in validate(env)), st


def test_receipt_none_needs_no_hash() -> None:
    from dataclasses import replace

    assert not any("receipt_state" in s for s in
                   validate(replace(_clean(), receipt_state=RECEIPT_NONE,
                                    rendered_bytes_hash="")))


def test_unknown_receipt_state_flagged() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), receipt_state="acknowledged",
                         rendered_bytes_hash="abc123"))
    assert any("receipt_state" in s for s in v)


# --------------------------------------------------------------------------- #
# DELIVERABLE 2 (f) — hash-shaped fields are lowercase hex or empty.
# --------------------------------------------------------------------------- #
def test_nonhex_parent_observation_hash_flagged() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), parent_observation_hash="zznothex"))
    assert any("parent_observation_hash" in s for s in v)


def test_nonhex_rendered_bytes_hash_flagged() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), receipt_state=RECEIPT_DELIVERED,
                         rendered_bytes_hash="NOTHEX!!"))
    assert any("rendered_bytes_hash" in s for s in v)


def test_uppercase_hash_flagged_lowercase_required() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), parent_observation_hash="ABCDEF"))
    assert any("parent_observation_hash" in s for s in v)


def test_valid_lowercase_hex_hash_clean() -> None:
    # F1 adjustment: valid hashes are FULL sha256 digests (64 lowercase hex) — the old
    # 16-hex/6-hex stand-ins are now width violations by design.
    from dataclasses import replace

    env = replace(_clean(), parent_observation_hash=hashlib.sha256(b"p").hexdigest(),
                  receipt_state=RECEIPT_DELIVERED,
                  rendered_bytes_hash=hashlib.sha256(b"r").hexdigest())
    assert not any("parent_observation_hash" in s or "rendered_bytes_hash" in s
                   for s in validate(env))


# --------------------------------------------------------------------------- #
# DELIVERABLE 2 (g) — delivery_reason and suppression_reason mutually exclusive.
# --------------------------------------------------------------------------- #
def test_delivery_and_suppression_mutually_exclusive() -> None:
    from dataclasses import replace

    v = validate(replace(_clean(), delivery_reason="fired", suppression_reason="stale"))
    assert any("delivery_reason" in s and "suppression_reason" in s for s in v)


def test_delivery_reason_alone_clean() -> None:
    from dataclasses import replace

    assert not any("suppression_reason" in s or "mutually" in s for s in
                   validate(replace(_clean(), delivery_reason="fired_at_decision")))


def test_suppression_reason_alone_clean() -> None:
    from dataclasses import replace

    assert not any("delivery_reason" in s or "mutually" in s for s in
                   validate(replace(_clean(), suppression_reason="acquired_already")))


# --------------------------------------------------------------------------- #
# DELIVERABLE 3 — render_bytes: LEAK LAW (9) + APPEND-ONLY LAW (1).
# --------------------------------------------------------------------------- #
def test_render_bytes_contains_only_payload_and_provenance() -> None:
    """LAW 9 (leak) at the new-field boundary: host-side metadata — even when it is
    a test-shaped path — is NEVER rendered; render_bytes ships ONLY payload lines +
    provenance rows, so the new fields cannot smuggle a leak."""
    from dataclasses import replace

    env = replace(
        _clean(
            payload=("def: src/users.py:120", "caller: src/api.py:44"),
            provenance=(("src/users.py", 120), ("src/api.py", 44)),
        ),
        renderer_id="tests/test_render_leak.py",       # test-shaped metadata
        delivery_reason="tests/test_users.py::fired",  # test-shaped metadata
        episode_id="tests/ep.py",
    )
    rb = render_bytes(env)
    # every payload line + provenance row is present (F2 adjustment: the commitment is
    # canonical JSON, so a provenance row serializes as ["file",line], not file:line) …
    assert b"def: src/users.py:120" in rb
    assert b'["src/api.py",44]' in rb
    assert b'["src/users.py",120]' in rb
    # … and NONE of the metadata fields leak into the shipped bytes.
    assert b"test_render_leak" not in rb
    assert b"test_users" not in rb
    assert b"tests/ep.py" not in rb
    assert b"fired" not in rb


def test_render_bytes_append_only_no_mutation() -> None:
    """LAW 1 (append-only): rendering an envelope NEVER mutates it — render twice ->
    identical bytes, and the envelope's serialized state is unchanged."""
    env = _clean()
    before = to_dict(env)
    b1 = render_bytes(env)
    b2 = render_bytes(env)
    assert b1 == b2
    assert isinstance(b1, bytes)
    assert to_dict(env) == before  # frozen: not mutated by rendering


def test_render_bytes_deterministic_single_process() -> None:
    """LAW 10 (byte-determinism) in-process: two independently-built identical
    envelopes render to byte-identical output."""
    a = _clean(payload=("l1", "l2", "l3"), provenance=(("src/a.py", 1), ("src/b.py", 2)))
    b = _clean(payload=("l1", "l2", "l3"), provenance=(("src/a.py", 1), ("src/b.py", 2)))
    assert render_bytes(a) == render_bytes(b)


def test_render_bytes_deterministic_two_processes(tmp_path) -> None:
    """LAW 10 (byte-determinism) across TWO processes with DIFFERENT PYTHONHASHSEED —
    a multi-line payload makes any set-iteration in the render path (mutation M2) bite;
    the non-ASCII line pins ensure_ascii=False stability on the F2 JSON serialization.
    Mirrors tests/runtime/test_gateway.py::test_determinism_two_processes."""
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f'''\
        import hashlib, sys
        sys.path.insert(0, {json.dumps(_repo_src())})
        from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, render_bytes
        env = EvidenceEnvelope.build(
            producer="def_ref_partition", fact_id="F1", target="src/users.py",
            evidence_type="def_ref_partition",
            payload=("alpha line", "bravo line", "caf\\u00e9 \\u2603 line", "delta line"),
            provenance=(("src/users.py", 120), ("src/models.py", 8), ("src/api.py", 44)),
            confidence=0.9, tier="VERIFIED", graph_revision="abc123",
            preferred_event="edit",
        )
        print(hashlib.sha256(render_bytes(env)).hexdigest())
    '''), encoding="utf-8")
    env0 = {**os.environ, "PYTHONHASHSEED": "0"}
    env1 = {**os.environ, "PYTHONHASHSEED": "1"}
    r0 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env0)
    r1 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env1)
    assert r0.returncode == 0, r0.stderr
    assert r1.returncode == 0, r1.stderr
    assert r0.stdout == r1.stdout
    assert r0.stdout.strip()  # non-empty: it actually rendered bytes


# --------------------------------------------------------------------------- #
# DELIVERABLE 3 — chain_hash: OBSERVATION-PREFIX HASH-CHAIN (LAW 5).
# --------------------------------------------------------------------------- #
def _chain(renders: "list[bytes]", seed: str = "") -> str:
    h = seed
    for r in renders:
        h = chain_hash(h, r)
    return h


def test_chain_hash_stable_and_documented() -> None:
    a = chain_hash("", b"hello")
    b = chain_hash("", b"hello")
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)  # sha256 lowercase hex


def test_chain_hash_depends_on_parent() -> None:
    """The prefix commitment: same rendered bytes under DIFFERENT parents -> different
    link (mutation M1, which drops the parent, makes these EQUAL). Parents are 64-hex
    (F1 adjustment: the old 2-hex parents are the closed forgery surface)."""
    assert chain_hash("a" * 64, b"same") != chain_hash("b" * 64, b"same")


def test_chain_replay_identical() -> None:
    a, b, c = (render_bytes(_clean(payload=(p,), provenance=(("src/x.py", i),)))
               for i, p in enumerate(("one", "two", "three"), start=1))
    assert _chain([a, b, c]) == _chain([a, b, c])  # (i) deterministic replay


def test_chain_reorder_changes_terminal() -> None:
    """(ii) reordering deliveries -> different terminal hash. The swap keeps the LAST
    delivery fixed, so a parent-ignoring chain (M1) would NOT detect it -> test bites."""
    a, b, c = (render_bytes(_clean(payload=(p,), provenance=(("src/x.py", i),)))
               for i, p in enumerate(("one", "two", "three"), start=1))
    assert _chain([a, b, c]) != _chain([b, a, c])


def test_chain_omission_changes_terminal() -> None:
    """(iii) omitting a middle link -> different terminal hash (tamper detection). The
    kept links still start with `a` and end with `c`, so only the chain commitment
    (not the terminal payload) reveals the dropped `b`."""
    a, b, c = (render_bytes(_clean(payload=(p,), provenance=(("src/x.py", i),)))
               for i, p in enumerate(("one", "two", "three"), start=1))
    assert _chain([a, b, c]) != _chain([a, c])


# --------------------------------------------------------------------------- #
# F1 (bounce, adversarial review 2026-07-10) — chain_hash boundary-shift forgery.
# Confirmed repro (scratchpad repro_g1r.py ATTACK 1): sha256(parent_hex.encode() +
# rendered) had NO domain separation, so ("ab", b"cdef") == ("a", b"bcdef"), and law
# (f) accepted ANY-length hex parent — enabling the shift. The fix: parent is EXACTLY
# 64 lowercase hex (decoded to its raw 32 bytes; empty => 32 zero bytes genesis), so
# the parent/rendered boundary is fixed-width-unambiguous, and a malformed parent
# RAISES (a corrupt chain fails loudly, never silently hashes).
# --------------------------------------------------------------------------- #
def test_chain_hash_rejects_non_64hex_parent() -> None:
    """F1: a non-empty parent that is not EXACTLY 64 lowercase hex -> ValueError.
    (Mutation M3 — dropping the width check — makes the even-length hex cases decode
    silently, so this test bites.)"""
    with pytest.raises(ValueError):
        chain_hash("a" * 63, b"x")            # one short of sha256 width
    with pytest.raises(ValueError):
        chain_hash("deadbeef", b"PAYLOAD")    # the exact forged-link parent (8-hex)
    with pytest.raises(ValueError):
        chain_hash("A" * 64, b"x")            # uppercase: not the canonical form


def test_chain_hash_boundary_shift_attack_dead() -> None:
    """F1: the exact confirmed collision pairs can no longer produce EQUAL hashes —
    both short parents now fail loudly, and the genesis link is domain-separated
    (32 zero bytes prefix) from a bare content hash, so the first-link forgery
    chain_hash('', b'deadbeefPAYLOAD') == chain_hash('deadbeef', b'PAYLOAD') is dead."""
    with pytest.raises(ValueError):
        chain_hash("ab", b"cdef")             # collision pair, left half
    with pytest.raises(ValueError):
        chain_hash("a", b"bcdef")             # collision pair, right half
    first = chain_hash("", b"deadbeefPAYLOAD")
    assert first != hashlib.sha256(b"deadbeefPAYLOAD").hexdigest()  # genesis-prefixed
    with pytest.raises(ValueError):
        chain_hash("deadbeef", b"PAYLOAD")    # the forged second link fails loudly


def test_chain_hash_valid_links_have_unambiguous_boundary() -> None:
    """F1: between two VALID links (64-hex parents), moving bytes across the
    parent/rendered boundary is impossible — distinct (parent, rendered) splits of
    the same concatenation hash differently because the parent is raw-32 fixed."""
    p1, p2 = "ab" * 32, "ba" * 32  # both valid 64-hex, same multiset of chars
    assert chain_hash(p1, b"same") != chain_hash(p2, b"same")


def test_validate_rejects_short_hex_parent_hash() -> None:
    """F1: law (f) pins WIDTH — the 8-hex 'deadbeef' that enabled the boundary shift
    is now a violation (hash fields are empty or EXACTLY 64 lowercase hex)."""
    from dataclasses import replace

    v = validate(replace(_clean(), parent_observation_hash="deadbeef"))
    assert any("parent_observation_hash" in s for s in v)


def test_validate_rejects_short_hex_rendered_hash() -> None:
    """F1: width pin applies to BOTH hash fields — rendered_bytes_hash too."""
    from dataclasses import replace

    v = validate(replace(_clean(), receipt_state=RECEIPT_DELIVERED,
                         rendered_bytes_hash="abc123"))
    assert any("rendered_bytes_hash" in s for s in v)


def test_validate_accepts_full_64hex_hashes() -> None:
    from dataclasses import replace

    h = hashlib.sha256(b"x").hexdigest()
    env = replace(_clean(), parent_observation_hash=h,
                  receipt_state=RECEIPT_DELIVERED, rendered_bytes_hash=h)
    assert not any("parent_observation_hash" in s or "rendered_bytes_hash" in s
                   for s in validate(env))


# --------------------------------------------------------------------------- #
# F2 (bounce, adversarial review 2026-07-10) — render_bytes injectivity.
# Confirmed repro (scratchpad repro_g1r2.py): "\n".join of unframed elements let
# payload=("a\nb",) collide with ("a","b") — identical bytes AND identical chain
# terminals, both envelopes validate-CLEAN — and a payload line was fungible with a
# provenance row (colon form). The fix: render_bytes is the canonical INJECTIVE
# chain-commitment serialization (canonical JSON of the STRUCTURED content).
# These are the reorder/omission suite's missing teeth — permanent law-5 pins.
# --------------------------------------------------------------------------- #
def test_render_bytes_injective_newline_split() -> None:
    """F2 pin: a payload line containing a newline is NOT the same content as two
    payload lines — different bytes AND different chain terminals (both envelopes
    validate-clean, exactly the confirmed severity)."""
    a = _clean(payload=("a\nb",), provenance=(), tier=INFO, confidence=0.4)
    b = _clean(payload=("a", "b"), provenance=(), tier=INFO, confidence=0.4)
    assert validate(a) == [] and validate(b) == []
    assert a.dedup_key != b.dedup_key  # distinct fact identities …
    assert render_bytes(a) != render_bytes(b)  # … must commit distinctly
    assert chain_hash("", render_bytes(a)) != chain_hash("", render_bytes(b))


def test_render_bytes_injective_payload_vs_provenance() -> None:
    """F2 pin: a payload LINE 'src/x.py:12' is NOT a provenance ROW ('src/x.py', 12) —
    the two content structures commit to different bytes (colon fungibility dead),
    including the mixed form from the review repro."""
    c = _clean(payload=("src/x.py:12",), provenance=(), tier=INFO, confidence=0.4)
    d = _clean(payload=(), provenance=(("src/x.py", 12),), tier=INFO, confidence=0.4)
    assert render_bytes(c) != render_bytes(d)
    e = _clean(payload=("caller: src/api.py:44", "src/x.py:12"),
               provenance=(("src/users.py", 120),), tier=INFO, confidence=0.4)
    f = _clean(payload=("caller: src/api.py:44",),
               provenance=(("src/users.py", 120), ("src/x.py", 12)),
               tier=INFO, confidence=0.4)
    assert validate(e) == [] and validate(f) == []
    assert render_bytes(e) != render_bytes(f)
    assert chain_hash("", render_bytes(e)) != chain_hash("", render_bytes(f))


def test_render_bytes_non_ascii_stable() -> None:
    """LAW 10 on the new serialization: non-ASCII content renders as raw UTF-8
    (ensure_ascii=False — one canonical byte form, no \\uXXXX escape drift) and is
    byte-identical across independently-built equal envelopes."""
    a = _clean(payload=("café ☃ delta",), provenance=(("src/uber.py", 7),))
    b = _clean(payload=("café ☃ delta",), provenance=(("src/uber.py", 7),))
    rb = render_bytes(a)
    assert rb == render_bytes(b)
    assert "café ☃ delta".encode("utf-8") in rb


# =========================================================================== #
# BOUNCE #2 (Fable LIPI review 2026-07-10, repro_w0_review.py) — W1/W2/W3/W4.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# W1 — lone-surrogate totality: build fails LOUDLY, validate NEVER raises,
# render_bytes raises a DOCUMENTED ValueError (never a raw UnicodeEncodeError).
# Realistic trigger: surrogateescape-decoded repo file content.
# --------------------------------------------------------------------------- #
def test_build_raises_clear_valueerror_on_lone_surrogate() -> None:
    """W1: a producer shipping an unencodable payload is a PRODUCER BUG — build
    catches it loudly at construction with a clear message, not a raw codec error."""
    with pytest.raises(ValueError) as ei:
        EvidenceEnvelope.build(producer="p", fact_id="f", target="t",
                               evidence_type="k", payload=("bad \ud800 line",))
    assert not isinstance(ei.value, UnicodeEncodeError)
    assert "UTF-8" in str(ei.value) and "build" in str(ei.value)


def test_build_raises_on_surrogateescape_decoded_content() -> None:
    """W1 realistic case: a non-UTF-8 repo file line decoded the common way
    (errors='surrogateescape') carries lone surrogates — build must refuse it."""
    line = b"caf\xe9 latin-1 line".decode("utf-8", errors="surrogateescape")
    with pytest.raises(ValueError) as ei:
        EvidenceEnvelope.build(producer="p", fact_id="f", target="t",
                               evidence_type="k", payload=(line,))
    assert not isinstance(ei.value, UnicodeEncodeError)


def test_validate_is_total_on_lone_surrogate() -> None:
    """W1: validate's contract is 'returns ALL violations (never raises)' — a
    type-conforming envelope with unencodable content must yield a VIOLATION
    string, not an exception (mutation M4 — re-raising instead — bites here)."""
    env = EvidenceEnvelope(producer="p", fact_id="f", target="t", evidence_type="k",
                           payload=("bad \ud800 line",), provenance=())
    v = validate(env)  # must not raise
    assert any("utf-8" in s.lower() and "encodable" in s.lower() for s in v)


def test_validate_total_on_surrogate_in_identity_fields() -> None:
    """W1: totality holds when the surrogate hides in an IDENTITY field (target)
    — the dedup recompute path must not blow up either."""
    env = EvidenceEnvelope(producer="p", fact_id="f", target="t\ud800",
                           evidence_type="k", payload=(), provenance=())
    v = validate(env)  # must not raise
    assert isinstance(v, list) and v  # at least the dedup mismatch fires


def test_render_bytes_raises_documented_valueerror_on_surrogate() -> None:
    """W1: render_bytes on unencodable content raises the DOCUMENTED ValueError
    with a clear message — never a raw UnicodeEncodeError."""
    env = EvidenceEnvelope(producer="p", fact_id="f", target="t", evidence_type="k",
                           payload=("bad \ud800 line",), provenance=())
    with pytest.raises(ValueError) as ei:
        render_bytes(env)
    assert not isinstance(ei.value, UnicodeEncodeError)
    assert "UTF-8" in str(ei.value)


# --------------------------------------------------------------------------- #
# W2 — dedup identity injectivity: content_signature commits to the SAME
# canonical-JSON bytes as render_bytes (hash of them); derive_dedup_key frames
# its 5 components injectively. The F2 flaw class on the OTHER surface: unframed
# joins let DISTINCT facts share a dedup key -> wrongful dedup suppression.
# --------------------------------------------------------------------------- #
def test_content_signature_nul_split_distinct() -> None:
    """W2 confirmed collision: payload ('a\\x00b',) vs ('a','b') had the SAME
    signature + SAME dedup key (renders already differ) — distinct facts must
    never share a dedup identity."""
    assert content_signature(("a\x00b",), ()) != content_signature(("a", "b"), ())
    n1 = _clean(payload=("a\x00b",), provenance=(), tier=INFO, confidence=0.4)
    n2 = _clean(payload=("a", "b"), provenance=(), tier=INFO, confidence=0.4)
    assert validate(n1) == [] and validate(n2) == []
    assert n1.dedup_key != n2.dedup_key


def test_content_signature_provenance_delimiter_injection_distinct() -> None:
    """W2 confirmed collision: provenance ('x:1;y', 2) vs ('x',1)+('y',2) — the
    ';'/':' join delimiters were forgeable from a FILE NAME."""
    assert (content_signature((), (("x:1;y", 2),))
            != content_signature((), (("x", 1), ("y", 2))))
    c1 = _clean(payload=(), provenance=(("x:1;y", 2),), tier=INFO, confidence=0.4,
                target="get_user")
    c2 = _clean(payload=(), provenance=(("x", 1), ("y", 2)), tier=INFO,
                confidence=0.4, target="get_user")
    assert c1.dedup_key != c2.dedup_key


def test_content_signature_equals_hash_of_render_bytes() -> None:
    """W2: ONE injective serialization — the signature is the sha256 of the EXACT
    render_bytes commitment bytes, so dedup identity and chain commitment can
    never disagree about what the content IS."""
    env = _clean()
    expected = hashlib.sha256(render_bytes(env)).hexdigest()
    assert content_signature(env.payload, env.provenance) == expected


def test_derive_dedup_key_field_boundary_shift_distinct() -> None:
    """W2 confirmed collision: the '\\x00'.join field framing let
    ('t\\x00x', 'f') collide with ('t', 'x\\x00f') — injective JSON framing of
    the 5 components kills the shift."""
    assert (derive_dedup_key("p", "k", "t\x00x", "f", "c")
            != derive_dedup_key("p", "k", "t", "x\x00f", "c"))


# --------------------------------------------------------------------------- #
# W3 — chain_hash rejects non-str parents (None coerced to genesis silently).
# --------------------------------------------------------------------------- #
def test_chain_hash_rejects_none_and_bytes_parent() -> None:
    """W3: chain_hash(None, ...) silently hashed as genesis — a corrupt chain
    input must fail loudly. None and bytes parents raise ValueError."""
    with pytest.raises(ValueError):
        chain_hash(None, b"x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        chain_hash(b"a" * 64, b"x")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# W4 — law-9 leak pin covers ALL NINE metadata fields (was 3 of 9).
# --------------------------------------------------------------------------- #
def test_render_bytes_excludes_all_nine_metadata_fields() -> None:
    """W4: every host-side metadata field carries a unique sentinel; NONE of
    their byte-forms may appear in the commitment bytes. (delivery/suppression
    both set violates law (g) — irrelevant here: render never validates.)"""
    from dataclasses import replace

    sentinels = {
        "episode_id": "EPISODE-SENTINEL-77",
        "event_id": "EVENT-SENTINEL-88",
        "parent_observation_hash": "abadcafe" * 8,      # sentinel-full 64-hex
        "payload_schema_version": "env.SENTINEL-99",
        "renderer_id": "RENDERER-SENTINEL-11",
        "rendered_bytes_hash": "deadbea7" * 8,          # sentinel-full 64-hex
        "receipt_state": RECEIPT_CAUSAL,                # legal value, distinctive
        "delivery_reason": "DELIVERY-SENTINEL-22",
        "suppression_reason": "SUPPRESSION-SENTINEL-33",
    }
    env = replace(_clean(), **sentinels)
    rb = render_bytes(env)
    assert b"def: src/users.py:120" in rb  # the payload IS committed …
    for field, sentinel in sentinels.items():
        assert sentinel.encode("utf-8") not in rb, f"{field} leaked into commitment"


def _repo_src() -> str:
    # tests/runtime/test_evidence_envelope.py -> repo/src
    return str(Path(__file__).resolve().parents[2] / "src")
