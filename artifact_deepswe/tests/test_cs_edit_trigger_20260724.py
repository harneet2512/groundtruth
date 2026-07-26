"""AUDIT 2026-07-24 — change_surface's OWN edit-path trigger (file CREATION).

MEASURED: newfile_precedent / GT_CHANGE_SURFACE emitted 0 rows across all 4 tasks of run
30121930273. Root cause is DISPATCH, not detection: change_surface was ONLY outcome-dispatched
(ZERO_ABSENT) and that route additionally requires failing the SAME search stem twice. An agent that
searches once, finds nothing, and creates the file never reaches it.

ADDITIVE by construction: obligations keeps task-start, patch_delta/caller_contract keep their edit
slots, the ZERO_ABSENT route is unchanged. Nothing is displaced.
"""
from __future__ import annotations
from groundtruth.runtime import gateway as gw


class _Ev:
    def __init__(self, mapping):
        self.edit_before_after = mapping


def test_creation_is_detected_structurally():
    """before empty + after non-empty == a creation. Language-agnostic, no verb list."""
    assert gw._event_creates_new_file(_Ev({"new.py": ("", "def f():\n    return 1\n")})) is True
    assert gw._event_creates_new_file(_Ev({"new.ts": (None, "export const x = 1\n")})) is True


def test_a_plain_modification_is_NOT_a_creation():
    """A modify-in-place must never masquerade as a creation (correct-or-quiet)."""
    assert gw._event_creates_new_file(_Ev({"old.py": ("def f(): pass\n", "def f(): return 1\n")})) is False
    assert gw._event_creates_new_file(_Ev({})) is False
    assert gw._event_creates_new_file(_Ev(None)) is False
    # a DELETION (content removed) is not a creation either
    assert gw._event_creates_new_file(_Ev({"gone.py": ("def f(): pass\n", "")})) is False


def test_trigger_flag_defaults_off(monkeypatch):
    """Byte-identical unless explicitly enabled."""
    monkeypatch.delenv("GT_CS_EDIT_TRIGGER", raising=False)
    assert gw._cs_edit_trigger_on() is False
    monkeypatch.setenv("GT_CS_EDIT_TRIGGER", "1")
    assert gw._cs_edit_trigger_on() is True


def test_zero_absent_route_still_requires_the_repeat(monkeypatch):
    """The historical search route is UNCHANGED — require_repeat defaults True."""
    import inspect
    sig = inspect.signature(gw._produce_change_surface)
    assert sig.parameters["require_repeat"].default is True, \
        "the ZERO_ABSENT caller must keep the repeat gate (byte-identical on that route)"


# ---------------------------------------------------------------------------
# END-TO-END: the tests above only prove the HELPER works. They do NOT prove
# `augment()` — the real dispatch entry every live observation goes through —
# actually REACHES the producer. That distinction is the entire bug this commit
# fixes (change_surface was reachable in the source yet unreachable in practice),
# and it is the same trap that made an earlier telemetry fix land dead behind the
# very gate it was meant to diagnose. So drive the real entry point.
# ---------------------------------------------------------------------------

def _drive(monkeypatch, mapping, *, trigger_on):
    """Call the REAL augment() with a real edit event; report whether the
    change_surface producer was reached. Producer is stubbed so the assertion is
    about DISPATCH, not about evidence content (which needs a populated graph)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    monkeypatch.setenv("GT_CS_EDIT_TRIGGER", "1" if trigger_on else "0")
    seen = []
    monkeypatch.setattr(
        gw, "_produce_change_surface",
        lambda ev, st, **kw: seen.append(kw.get("require_repeat", True)) or [],
    )
    ev = gw.ToolEvent(kind=gw.KIND_EDIT, command="create", action_index=1,
                      changed_files=tuple(mapping), edit_before_after=mapping)
    gw.augment(ev, gw.GatewayState())
    return seen


def test_e2e_creation_reaches_the_producer_through_real_augment(monkeypatch):
    """A file CREATION must reach change_surface via the real dispatch."""
    seen = _drive(monkeypatch, {"pkg/new_mod.py": ("", "def f():\n    return 1\n")}, trigger_on=True)
    assert seen == [False], (
        "creation did not reach _produce_change_surface through augment() — the producer is "
        f"defined and called in source but unreachable at runtime (calls={seen})"
    )


def test_post_creation_missing_role_survives_production_registry(
    tmp_path, monkeypatch,
):
    """A producer call is not a fire.  Under Profile-2 registry enforcement the
    post-creation integration fact must survive route_delivery and become an
    admitted model-facing envelope at the edit boundary."""
    from types import SimpleNamespace

    for flag in (
        "GT_GATEWAY", "GT_CHANGE_SURFACE", "GT_CS_EDIT_TRIGGER",
        "GT_REGISTRY_ENFORCE",
    ):
        monkeypatch.setenv(flag, "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    missing = SimpleNamespace(
        role="handler",
        entity="baz",
        registration_file="registry.py",
        sibling_files=(),
        registration_lines=(),
        evidence=("registry.py has sibling handlers but no baz entry",),
    )
    result = SimpleNamespace(
        abstained=False,
        destinations=(),
        missing_roles=(missing,),
        sibling_groups=(),
    )
    monkeypatch.setattr(gw, "detect_change_surface", lambda *a, **k: result)

    event = gw.ToolEvent(
        kind=gw.KIND_EDIT,
        command="cat <<'EOF' > handlers/baz.py",
        action_index=3,
        changed_files=("handlers/baz.py",),
        edit_before_after={"handlers/baz.py": (None, "class Baz:\n    pass\n")},
    )
    state = gw.GatewayState(
        repo_root=str(tmp_path),
        issue_text="add baz and register it with the sibling handlers",
    )

    admitted = gw.augment(event, state)

    assert [env.evidence_type for env in admitted] == [
        "missing_role_postcreate:handler"
    ], (
        "post-creation integration evidence was produced but expired by the "
        "failed_search contract before it could fire"
    )
    from groundtruth.runtime import global_arbiter
    from groundtruth.runtime.adapters import miniswe
    assert global_arbiter.class_of_kind(admitted[0].evidence_type) == "localization"
    assert miniswe._priority(admitted[0])[1] == miniswe._EVIDENCE_TYPE_RANK[
        "missing_role_postcreate"
    ]
    rendered = miniswe.render_envelope(admitted[0], native=True)
    assert rendered and "registry.py" in rendered
    assert "<gt-" not in rendered


def test_e2e_modification_stays_quiet_through_real_augment(monkeypatch):
    """Correct-or-quiet: a modify-in-place must NOT reach it (no new dose)."""
    seen = _drive(monkeypatch, {"pkg/old.py": ("def f(): pass\n", "def f(): return 1\n")}, trigger_on=True)
    assert seen == [], f"modification must not trigger change_surface (calls={seen})"


def test_e2e_flag_off_is_byte_identical_through_real_augment(monkeypatch):
    """Default-OFF must be provably inert on the very same creating event."""
    seen = _drive(monkeypatch, {"pkg/new_mod.py": ("", "x = 1\n")}, trigger_on=False)
    assert seen == [], f"flag off must not reach the producer (calls={seen})"


# ---------------------------------------------------------------------------
# CORRECT-OR-QUIET: the edit-path trigger fires right AFTER the agent created a
# file, but detect_change_surface derives destinations from issue_text + the
# PRE-TASK graph.db and its only "already present" test is entity membership in a
# registry (change_surface.py:952) — it cannot see a file created seconds ago. So
# it can emit "new file: X" about a path that now exists: redundant at best, and
# at worst it names a DIFFERENT path and reads as "you created the wrong file."
#
# The first cut of this filter used `Path`, which is NOT imported in gateway, and
# the NameError was swallowed by a broad `except Exception` — the filter silently
# returned False always and the fix was DEAD while looking present. These tests
# assert it is FUNCTIONALLY live, which a name/import check cannot do.
# ---------------------------------------------------------------------------

def test_existing_destination_is_detected(tmp_path):
    (tmp_path / "made.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.py").write_text("y = 1\n")

    class _S:
        repo_root = str(tmp_path)

    assert gw._destination_already_exists("made.py", _S()) is True
    assert gw._destination_already_exists("sub/nested.py", _S()) is True, \
        "nested paths must resolve — destinations are repo-relative"
    assert gw._destination_already_exists("absent.py", _S()) is False
    assert gw._destination_already_exists("sub", _S()) is False, \
        "a DIRECTORY is not an existing file destination"
    assert gw._destination_already_exists("", _S()) is False


def test_filter_is_not_silently_dead(tmp_path):
    """Guards the exact regression above: if the body raises internally it returns
    False for everything, which is indistinguishable from 'nothing exists'. Prove a
    real file returns True, so a swallowed NameError can never pass this test."""
    (tmp_path / "real.py").write_text("x = 1\n")

    class _S:
        repo_root = str(tmp_path)

    assert gw._destination_already_exists("real.py", _S()) is True, \
        "filter is dead — a swallowed exception is disabling it"


def test_missing_field_or_bad_root_stays_quiet():
    assert gw._destination_already_exists("x.py", type("_S", (), {"repo_root": None})()) is False
    assert gw._destination_already_exists("x.py", type("_S", (), {})()) is False


def test_zero_absent_route_does_not_drop_destinations():
    """Byte-identity on the historical search route: the drop is OPT-IN."""
    import inspect
    sig = inspect.signature(gw._produce_change_surface)
    assert sig.parameters["drop_existing_destinations"].default is False
