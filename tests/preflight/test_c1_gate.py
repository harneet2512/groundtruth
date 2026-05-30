"""Cluster-1 SAFE-RENDER gate — generalized adversarial coverage (A-E).

The gate runs INDEPENDENT semantic checks, not just sanitizer idempotence (an
incomplete sanitizer would otherwise pass bad content). General rules are
structural/language-agnostic. The harness file-read banners
(`Here's the result of running`, `cat -n`, `# SPDX`) are used ONLY as artifact
regression fixtures — markerless glue is PREVENTED at the boundary
(sanitizer.join_without_glue), never asserted as a general gate rule.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "verify"
sys.path.insert(0, str(_SCRIPTS))
import check_brief_delivery as cbd  # noqa: E402
from groundtruth.runtime.sanitizer import join_without_glue, sanitize_evidence_block  # noqa: E402


def _instr(contract_lines: str) -> str:
    return "<gt-task-brief>\n" + contract_lines + "\n</gt-task-brief>\n<uploaded_files>\nrepo\n</uploaded_files>\nissue"


# ===== A. Bad exception extraction (rule 1) =====
@pytest.mark.parametrize("seg", [
    "raises raise", "raises raise,exc_info[1].with_traceback", "raises (lambda: None)()",
    "raises dict()", "raises exc_info[1]"])
def test_A_bad_exception_flagged(seg):
    assert cbd._scan_exception_specs(_instr(f"Contract: {seg}")), f"must flag: {seg}"


@pytest.mark.parametrize("seg", [
    "raises TypeError", "raises ValueError", "raises FileNotFoundError",
    "raises module.CustomError", "raises TypeError,ValueError"])
def test_A_valid_exception_not_flagged(seg):
    assert not cbd._scan_exception_specs(_instr(f"Contract: {seg}")), f"must NOT flag: {seg}"


# ===== B. Empty / placeholder fields (rule 2) =====
@pytest.mark.parametrize("line", [
    "Contract: raises", "Contract: returns", "Preserve: guard_clause:",
    "Preserve: guard_clause:   ", "Preserve: return_shape:", "Preserve:"])
def test_B_empty_field_flagged(line):
    assert cbd._scan_empty_fields(line), f"must flag empty: {line!r}"


@pytest.mark.parametrize("line", [
    "Preserve: guard_clause: raise: not isinstance(documents, list)",
    "Contract: raises TypeError,ValueError", "Contract: returns value|entries"])
def test_B_valid_field_not_flagged(line):
    assert not cbd._scan_empty_fields(line), f"must NOT flag valid: {line!r}"


# ===== C. Glue (rule 5 = general truncated-marker; markerless = boundary) =====
@pytest.mark.parametrize("s", ["[CATCHEHere's", "[RAISEStuff", "[CONTRAClower"])
def test_C_truncated_marker_flagged(s):
    assert cbd._scan_truncated_markers(s), f"must flag truncated marker: {s!r}"


@pytest.mark.parametrize("s", [
    "[CATCHES] Here's", "[BEHAVIORAL CONTRACT]", "[GT_VERIFY] Tests covering", "[GT] Post-edit:"])
def test_C_intact_marker_not_flagged(s):
    assert not cbd._scan_truncated_markers(s), f"must NOT flag intact marker: {s!r}"


@pytest.mark.parametrize("s", [
    "[Optional]", "List[Document]", "Dict[str, Any]", "Tuple[int, str]",
    "Optional[User]", "items: List[Document]", "def run(self) -> Dict[str, Any]:"])
def test_C_type_hints_not_flagged_as_truncated_markers(s):
    """Regression for the 115-false-positive bug: ordinary bracketed text / type
    hints must NEVER be flagged as a cut GT marker (the detector is anchored on
    the GT marker name set)."""
    assert not cbd._scan_truncated_markers(s), f"type hint false-flagged: {s!r}"


@pytest.mark.parametrize("l,r", [("text wit", "# SPDX"), ("split_ove", "Here's"), ("join(tm)", "Here's")])
def test_C_markerless_glue_prevented_at_boundary(l, r):
    """Markerless glue has no general gate signature; it is PREVENTED at the
    boundary (this is a regression for join_without_glue, not a gate rule)."""
    joined = join_without_glue(l, r)
    assert "\n" in joined and (l + r) not in joined, f"boundary must insert newline: {joined!r}"


# ===== D. Unsafe truncation (rules 3,4 — reuse existing well-formed check) =====
@pytest.mark.parametrize("v", ["x and", "y or", "z not", '"DocumentSplitter expects'])
def test_D_dangling_or_unterminated_flagged(v):
    assert not cbd._clause_is_well_formed(v), f"must flag malformed: {v!r}"


@pytest.mark.parametrize("v", ['"closed string"', "not isinstance(x, list)", "a … b"])
def test_D_wellformed_not_flagged(v):
    assert cbd._clause_is_well_formed(v), f"must NOT flag: {v!r}"


# ===== E. Idempotence (rule 6 — final backstop, runs after independent checks) =====
def test_E_dirty_brief_not_idempotent():
    region = "<gt-task-brief>\nContract: raises raise,exc_info[1].with_traceback\n</gt-task-brief>"
    assert sanitize_evidence_block(region) != region, "Safe Renderer must change a dirty brief"


def test_E_clean_brief_is_idempotent():
    region = "<gt-task-brief>\nContract: raises TypeError,ValueError\n</gt-task-brief>"
    assert sanitize_evidence_block(region) == region, "Safe Renderer must not change a clean brief"


# ===== Independence: the gate catches bad content WITHOUT relying on idempotence =====
def test_independence_gate_catches_without_idempotence():
    """Rules 1/2/5 flag bad content directly; idempotence is only the backstop."""
    instr = _instr("Contract: raises raise,exc_info[1].with_traceback | preserve x")
    assert cbd._scan_exception_specs(instr)          # rule 1 fires
    instr2 = _instr("Contract: raises")
    assert cbd._scan_empty_fields(instr2)            # rule 2 fires


# ===== Full check_brief_delivery on a synthetic output.jsonl =====
def _write_jsonl(tmp_path, instruction, obs_content=""):
    rec = {"instance_id": "synthetic",
           "history": [{"source": "user", "content": instruction}]}
    if obs_content:
        rec["history"].append({"observation": "run", "content": obs_content})
    p = tmp_path / "output.jsonl"
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(p)


def test_full_gate_red_on_bad_brief(tmp_path):
    instr = _instr("Contract: raises raise,exc_info[1].with_traceback")
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr), require_safe_render=True)
    assert not r["passed"]
    assert r["bad_exception_specs"], r
    assert r["brief_idempotent"] is False


def test_full_gate_green_on_clean_brief(tmp_path):
    instr = _instr("Contract: raises TypeError,ValueError | returns value|entries")
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr), require_safe_render=True)
    assert r["passed"], r["reasons"]
    assert r["brief_idempotent"] is True


def test_full_gate_red_on_observation_glue(tmp_path):
    """Artifact regression: the beets `[CATCHEHere's` glue lived in an OBSERVATION."""
    instr = _instr("Contract: raises TypeError")
    obs = 'need at least one item") | [CATCHEHere\'s the result of running cat -n'
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr, obs_content=obs), require_safe_render=True)
    assert not r["passed"]
    assert r["truncated_markers"], r


def test_obs_guard_with_trailing_gt_tag_not_flagged(tmp_path):
    """Regression from live canary 26675541293 (beets): a VALID guard followed by
    a structural closing tag (`</gt-context>`) must NOT be flagged malformed — the
    tag is stripped before well-formedness validation. The `>` of the tag is not a
    dangling operator."""
    instr = _instr("Contract: raises TypeError")
    obs = ("[CATCHES] except ImportError -> handles | "
           "[CATCHES] except ImportError -> handles</gt-context>")
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr, obs_content=obs), require_safe_render=True)
    assert r["passed"], r["reasons"]
    assert not r["malformed_observation_guards"], r["malformed_observation_guards"]


def test_obs_guard_real_malformed_still_caught(tmp_path):
    """Negative control: stripping trailing tags must NOT hide a genuinely
    malformed guard (unterminated string) that happens to precede a tag."""
    instr = _instr("Contract: raises TypeError")
    obs = '[RAISES] raise ValueError("unterminated literal</gt-context>'
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr, obs_content=obs), require_safe_render=True)
    assert not r["passed"], "an unterminated string must still be flagged"


def test_full_gate_backward_compatible_default(tmp_path):
    """Without --require-safe-render, a dirty brief still passes the legacy gate
    (the new checks are opt-in, computed-not-enforced)."""
    instr = _instr("Contract: raises raise,exc_info[1].with_traceback")
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr))  # no flag
    assert r["passed"], "default behavior unchanged; new checks are opt-in"
    assert r["bad_exception_specs"], "but still COMPUTED for visibility"


# ===== Gate precision — real false-positives surfaced by the UNSEEN weasyprint task =====
@pytest.mark.parametrize("s", [
    "e => new_box = result[0]\n[CONTRACT ~] possible callers of block_b",   # verbatim weasyprint bytes
    "[RAISES ~] possible callers", "[CATCHES ?] maybe-handler",
    "[CONTRACT ~]", "[SIGNATURE ~] approx sig"])
def test_modifier_marker_not_flagged(s):
    """A marker with a ` ~]`/` ?]` modifier suffix is INTACT (GT marks *approximate* callers with
    `~`), not a cut. The cut/glue signature is a marker name fused to an alphanumeric WORD
    (`[CATCHEHere`), never a space+modifier."""
    assert not cbd._scan_truncated_markers(s), f"modifier marker false-flagged: {s!r}"


def test_multiline_balanced_guard_not_flagged(tmp_path):
    """A balanced guard split across physical lines must NOT be flagged malformed — the per-line
    extraction was splitting it at the dangling `or` (real weasyprint observation)."""
    instr = _instr("Contract: raises TypeError")
    obs = ("  PRESERVE: return: (box.style['column_width'] != 'auto' or\n"
           "            box.style['column_count'] != 'auto') -> result = columns_layout")
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr, obs_content=obs), require_safe_render=True)
    assert r["passed"], r["reasons"]
    assert not r["malformed_observation_guards"], r["malformed_observation_guards"]


def test_genuinely_unterminated_multiline_guard_still_flagged(tmp_path):
    """Negative control: a guard that stays malformed even after joining continuation lines
    (unterminated string) must STILL flag — multi-line tolerance must not hide real defects."""
    instr = _instr("Contract: raises TypeError")
    obs = ('  PRESERVE: return: (box.style["column_width"] != "auto and\n'
           '            more text that never closes the string literal')
    r = cbd.check_brief_delivery(_write_jsonl(tmp_path, instr, obs_content=obs), require_safe_render=True)
    assert not r["passed"], "an unterminated multi-line guard must still be flagged"
