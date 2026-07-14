"""Successful-write truth at the mini-swe observation seam.

The command result's normalized return code is authoritative when present.  Textual
failure markers exist only for older/replay observations that genuinely lack one.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402

_MISSING = object()


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    monkeypatch.delenv("GT_SS_RECOVERY_V2", raising=False)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    g._reset_oracle_state()
    # Production seeds the byte baseline before env.execute.  Direct seam tests do
    # the same explicitly so a first-command edit has a real pre-image.
    g._subprocess_write_targets(str(tmp_path))


def _observe(cmd: str, output: str, returncode: Any = _MISSING):
    out = {"output": output}
    if returncode is not _MISSING:
        out["returncode"] = returncode
    g._augment_output({"command": cmd}, out)


def _writes() -> list[tuple[str, int, bool]]:
    return [event for event in g._ss_edit_events if event[0] == "pkg/mod.py"]


def _rewrite(root, text: str) -> None:
    (root / "pkg" / "mod.py").write_text(text, encoding="utf-8")


def test_real_seam_nonzero_silent_write_is_not_successful():
    _observe("sed -i 's/1/2/' pkg/mod.py", "", 7)

    assert _writes() == [("pkg/mod.py", 1, False)]
    assert g._ss_coherence_churn("pkg/mod.py") is None


def test_real_seam_zero_returncode_is_authoritative_over_text_marker(tmp_path):
    _rewrite(tmp_path, "value = 2\n")
    _observe("sed -i 's/1/2/' pkg/mod.py", "permission denied", 0)

    assert _writes() == [("pkg/mod.py", 1, True)]


def test_absent_returncode_uses_textual_compatibility_fallback(tmp_path):
    _rewrite(tmp_path, "value = 2\n")
    _observe("sed -i 's/1/2/' pkg/mod.py", "")
    _observe("sed -i 's/2/3/' pkg/mod.py", "patch failed")

    assert _writes() == [
        ("pkg/mod.py", 1, True),
        ("pkg/mod.py", 2, False),
    ]


def test_latest_genuine_pass_resets_chronology_and_failed_write_does_not_count(tmp_path):
    _rewrite(tmp_path, "value = 2\n")
    _observe("sed -i 's/1/2/' pkg/mod.py", "", 0)      # before green
    _observe("pytest -q", "1 passed", 0)               # genuine passing checkpoint
    _rewrite(tmp_path, "value = 3\n")
    _observe("sed -i 's/2/3/' pkg/mod.py", "", 0)
    _rewrite(tmp_path, "value = 4\n")
    _observe("sed -i 's/3/4/' pkg/mod.py", "", 0)
    _observe("sed -i 's/4/5/' pkg/mod.py", "", 9)      # silent failure: excluded
    _rewrite(tmp_path, "value = 5\n")
    _observe("sed -i 's/4/5/' pkg/mod.py", "", 0)

    assert [ok for _, _, ok in _writes()] == [True, True, True, False, True]
    assert g._ss_coherence_churn("pkg/mod.py") == 3


def test_failed_write_does_not_reset_recovery_repeat_streak(monkeypatch):
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._ss_record_test("pytest -q", "1 failed", True, False)
    g._ss_record_test("pytest -q", "1 failed", True, False)
    assert g._ss_recovery_eligible() is True

    g._ss_record_edit("pkg/mod.py", "sed -i 's/1/2/' pkg/mod.py", "", returncode=3)

    assert g._ss_recovery_eligible() is True


def test_absent_returncode_preserves_historical_recovery_reset(monkeypatch):
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._ss_record_test("pytest -q", "1 failed", True, False)
    g._ss_record_test("pytest -q", "1 failed", True, False)

    g._ss_record_edit("pkg/mod.py", "sed -i 's/1/2/' pkg/mod.py", "patch failed")

    assert g._ss_recovery_eligible() is False


def test_non_write_command_remains_excluded_with_known_returncode(monkeypatch):
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._ss_record_test("pytest -q", "1 failed", True, False)
    g._ss_record_test("pytest -q", "1 failed", True, False)

    g._ss_record_edit("pkg/mod.py", "cat pkg/mod.py", "", returncode=0)

    assert _writes() == []
    assert g._ss_recovery_eligible() is True


def test_real_seam_rc_zero_noop_is_nonchurn_and_not_post_edit():
    _observe("sed -i 's/missing/replacement/' pkg/mod.py", "", 0)

    assert _writes() == [("pkg/mod.py", 1, False)]
    assert g._ss_coherence_churn("pkg/mod.py") is None
    assert g._source_edit_count == 0
    assert g._oracle_edited_rels == set()
    assert g._edit_churn == {}


def test_real_seam_same_byte_rewrite_is_nonchurn_and_not_post_edit(tmp_path):
    # Command success alone is only a write attempt. Without changed bytes it must
    # neither inflate coherence nor unlock byte-dependent post-edit producers.
    _rewrite(tmp_path, "value = 1\n")
    _observe("python -c \"open('pkg/mod.py','w').write('value = 1\\n')\"", "", 0)

    assert _writes() == [("pkg/mod.py", 1, False)]
    assert g._source_edit_count == 0
    assert g._oracle_edited_rels == set()


def test_same_byte_success_does_not_reset_recovery_repeat_streak(monkeypatch):
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._ss_record_test("pytest -q", "1 failed", True, False)
    g._ss_record_test("pytest -q", "1 failed", True, False)

    g._ss_record_edit(
        "pkg/mod.py", "python -c \"open('pkg/mod.py','w').write('same')\"", "",
        returncode=0, bytes_changed=False,
    )

    assert _writes() == [("pkg/mod.py", 0, False)]
    assert g._ss_recovery_eligible() is True


def test_same_byte_success_preserves_legacy_off_write_event(monkeypatch):
    monkeypatch.delenv("GT_SS_COHERENCE_V2", raising=False)
    monkeypatch.delenv("GT_SS_RECOVERY_V2", raising=False)

    g._ss_record_edit(
        "pkg/mod.py", "python -c \"open('pkg/mod.py','w').write('same')\"", "",
        returncode=0, bytes_changed=False,
    )

    assert _writes() == [("pkg/mod.py", 0, True)]


@pytest.mark.parametrize("invalid_rc", [False, 0.0, "0"])
def test_returncode_requires_strict_integer_not_bool_float_or_string(invalid_rc):
    assert g._ss_write_ok("", returncode=invalid_rc) is False


def test_wrapped_execute_seeds_preimage_before_first_edit(monkeypatch, tmp_path):
    # The production wrapper, unlike direct unit calls, must seed the first pre-image
    # itself; otherwise a task whose first action is an edit is silently under-counted.
    g._reset_oracle_state()

    def original(_env, _action):
        _rewrite(tmp_path, "value = 2\n")
        return {"output": "", "returncode": 0}

    wrapped = g._wrap_execute(original)
    out = wrapped(object(), {"command": "sed -i 's/1/2/' pkg/mod.py"})

    assert out["returncode"] == 0
    assert _writes() == [("pkg/mod.py", 1, True)]


def test_direct_replay_hook_captures_preimage_before_materialization(tmp_path):
    g._reset_oracle_state()
    action = {"command": "sed -i 's/1/2/' pkg/mod.py"}

    g._ss_capture_write_preimage(action)
    _rewrite(tmp_path, "value = 2\n")
    out = {"output": "", "returncode": 0}
    g._augment_output(action, out)

    assert _writes() == [("pkg/mod.py", 1, True)]


def test_failed_initial_snapshot_does_not_fabricate_post_edit(monkeypatch):
    g._reset_oracle_state()
    real_hash = g._source_content_sha256
    monkeypatch.setattr(g, "_source_content_sha256", lambda _path: "")
    g._subprocess_write_targets(g._root(), content_proof=True)
    monkeypatch.setattr(g, "_source_content_sha256", real_hash)

    _observe("sed -i 's/missing/replacement/' pkg/mod.py", "", 0)

    # Strict rc=0 proves only that the write attempt ran. Without a trustworthy
    # pre/post snapshot, V2 must not fabricate a landed byte transition.
    assert _writes() == [("pkg/mod.py", 1, False)]
    assert g._oracle_edited_rels == set()


def test_byte_proven_copy_and_delete_are_successful_writes(tmp_path):
    _rewrite(tmp_path, "value = 2\n")
    _observe("cp donor.py pkg/mod.py", "", 0)
    assert _writes() == [("pkg/mod.py", 1, True)]

    (tmp_path / "pkg" / "mod.py").unlink()
    _observe("rm pkg/mod.py", "", 0)
    assert _writes()[-1] == ("pkg/mod.py", 2, True)


def test_known_target_preimage_detects_metadata_preserving_first_write(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    monkeypatch.setattr(g, "_GT_MTIME_SCAN_CAP", 1)
    g._reset_oracle_state()
    target = tmp_path / "pkg" / "mod.py"
    before = target.stat()

    def original(_env, _action):
        target.write_text("value = 2\n", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return {"output": "", "returncode": 0}

    g._wrap_execute(original)(object(), {"command": "sed -i 's/1/2/' pkg/mod.py"})

    assert _writes() == [("pkg/mod.py", 1, True)]


def test_hidden_multi_file_command_records_every_changed_path(tmp_path):
    (tmp_path / "pkg" / "other.py").write_text("other = 1\n", encoding="utf-8")
    g._reset_oracle_state()
    g._subprocess_write_targets(str(tmp_path), content_proof=True)
    _rewrite(tmp_path, "value = 2\n")
    (tmp_path / "pkg" / "other.py").write_text("other = 2\n", encoding="utf-8")

    _observe("custom-codegen", "", 0)

    assert sorted((rel, ok) for rel, _, ok in g._ss_edit_events) == [
        ("pkg/mod.py", True),
        ("pkg/other.py", True),
    ]
    assert g._oracle_edited_rels == {"pkg/mod.py", "pkg/other.py"}


def test_noop_parsed_target_does_not_hide_second_changed_path(tmp_path):
    """A parser hit is only an attempted target; byte truth may land elsewhere."""
    other = tmp_path / "pkg" / "other.py"
    other.write_text("other = 1\n", encoding="utf-8")
    g._reset_oracle_state()
    action = {
        "command": (
            "python -c \"open('pkg/mod.py','w').write('value = 1\\n'); "
            "open('pkg/other.py','w').write('other = 2\\n')\""
        )
    }

    g._ss_capture_write_preimage(action)
    before = other.stat()
    other.write_text("other = 2\n", encoding="utf-8")
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    g._augment_output(action, {"output": "", "returncode": 0})

    assert [(rel, ok) for rel, _, ok in g._ss_edit_events] == [
        ("pkg/other.py", True),
    ]
    assert g._oracle_edited_rels == {"pkg/other.py"}


def test_multi_file_delete_records_every_deleted_source(tmp_path):
    first = tmp_path / "pkg" / "first.py"
    second = tmp_path / "pkg" / "second.py"
    first.write_text("first = 1\n", encoding="utf-8")
    second.write_text("second = 1\n", encoding="utf-8")
    g._reset_oracle_state()
    action = {"command": "rm pkg/first.py pkg/second.py"}

    g._ss_capture_write_preimage(action)
    first.unlink()
    second.unlink()
    g._augment_output(action, {"output": "", "returncode": 0})

    assert sorted((rel, ok) for rel, _, ok in g._ss_edit_events) == [
        ("pkg/first.py", True),
        ("pkg/second.py", True),
    ]
    assert g._oracle_edited_rels == {"pkg/first.py", "pkg/second.py"}


def test_bounded_snapshot_is_explicitly_incomplete_and_deterministic(monkeypatch, tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    monkeypatch.setattr(g, "_GT_MTIME_SCAN_CAP", 1)

    first = g._scan_source_state(str(tmp_path), content_proof=True)
    second = g._scan_source_state(str(tmp_path), content_proof=True)

    assert first.complete is False
    assert list(first.state) == list(second.state) == [str(tmp_path / "a.py")]


def test_v2_unknown_byte_state_fails_closed():
    g._ss_record_edit(
        "pkg/mod.py", "sed -i 's/1/2/' pkg/mod.py", "", returncode=0,
        bytes_changed=None,
    )

    assert _writes() == [("pkg/mod.py", 0, False)]


def test_host_absolute_relpath_normalizes_to_repo_identity(monkeypatch):
    """The helper, not its callers, owns canonical graph-key separators."""
    monkeypatch.setattr(g.os.path, "relpath", lambda _path, _root: r"pkg\mod.py")

    assert g._to_repo_rel("/outside/pkg/mod.py", "/repo") == "pkg/mod.py"


def test_relative_backslash_path_normalizes_to_repo_identity():
    """An already-relative Windows observation must match the POSIX graph key."""
    assert g._to_repo_rel(r"pkg\mod.py", "/repo") == "pkg/mod.py"


def test_flag_off_wrapper_performs_no_content_preimage_reads(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")
    g._reset_oracle_state()

    reads = []

    def counted(path):
        reads.append(path)
        return "unexpected"

    monkeypatch.setattr(g, "_source_content_sha256", counted)
    wrapped = g._wrap_execute(
        lambda _env, _action: {"output": "same bytes", "returncode": 0})

    assert wrapped(object(), {"command": "true"})["output"] == "same bytes"
    assert reads == []


def test_v2_read_only_observation_performs_no_content_reads(monkeypatch):
    g._reset_oracle_state()
    reads = []
    monkeypatch.setattr(
        g, "_source_content_sha256",
        lambda path: reads.append(path) or "unexpected",
    )
    wrapped = g._wrap_execute(
        lambda _env, _action: {"output": "value = 1", "returncode": 0})

    out = wrapped(object(), {"command": "cat pkg/mod.py"})

    assert out["output"] == "value = 1"
    assert reads == []


def test_flag_off_returncode_does_not_change_model_visible_bytes(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")

    observed = []
    for rc in (0, 9):
        g._reset_oracle_state()
        out = {"output": "command observation", "returncode": rc}
        g._augment_output({"command": "sed -i 's/1/2/' pkg/mod.py"}, out)
        observed.append(out["output"])

    assert observed[0].encode("utf-8") == observed[1].encode("utf-8")
