"""P5 REMAINDER — surgical fixes across the runtime engines.

Groups (one section each): C-1/C-3 (context_budget), L-2 (traces SyntaxError frame),
O-2/O-3 (obligations freshness + hash width). Each asserts the exact behaviour the
finding named; RED on the pre-fix code.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "artifact_deepswe")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ============================================================================ #
# C-1 — trim preserves the PRODUCER's line order in the output
# ============================================================================ #
def test_c1_trim_preserves_producer_order():
    from groundtruth.runtime.context_budget import ContextBudgeter
    b = ContextBudgeter()
    # producer order: a FACT line first, an IMPERATIVE line second.
    payload = "[WITNESS] get_user called by -> b/y.py:4\nInspect a/x.py before editing"
    out = b.trim(payload, 500)
    lines = out.text.split("\n")
    assert lines[0].startswith("[WITNESS]")     # producer order preserved ...
    assert lines[1].startswith("Inspect")        # ... not reordered imperative-first


def test_c1_budget_clip_still_prefers_imperative():
    """Under a TIGHT budget that fits only one line, the actionable imperative is the
    one KEPT (priority selection survives) — but a kept set is emitted in producer
    order."""
    from groundtruth.runtime.context_budget import ContextBudgeter
    b = ContextBudgeter()
    # each line ~40 chars; max_tokens=10 -> limit=40 chars -> only ONE line fits.
    payload = "[WITNESS] get_user called by -> b/y.py:4444\nInspect a/x.py now please"
    out = b.trim(payload, 10)
    assert out.text.count("\n") == 0            # only one line survived the clip
    assert out.text.startswith("Inspect")        # the imperative was preferred


# ============================================================================ #
# C-3 — stable_fact_id discriminates distinct facts sharing tag+symbol
# ============================================================================ #
def test_c3_distinct_facts_same_tag_symbol_get_distinct_ids():
    from groundtruth.runtime.context_budget import stable_fact_id
    a = stable_fact_id("[WITNESS] get_user calls -> a/x.py:1")
    b = stable_fact_id("[WITNESS] get_user called by -> b/y.py:4")
    assert a != b                                # no collision on witness:get_user
    assert a.startswith("WITNESS:get_user:")     # structured, readable prefix kept


def test_c3_identical_render_still_collides():
    from groundtruth.runtime.context_budget import stable_fact_id
    a = stable_fact_id("[WITNESS] get_user called by -> b/y.py:4")
    b = stable_fact_id("[WITNESS]  get_user   called by  ->  b/y.py:4")  # whitespace
    assert a == b                                # true dedup: same fact re-rendered


def test_c3_second_distinct_fact_not_suppressed():
    """The exact C-3 bug: after delivering fact A, a DISTINCT fact B sharing tag+sym
    must NOT be dropped by the fresh-filter."""
    from groundtruth.runtime.context_budget import ContextBudgeter
    b = ContextBudgeter()
    a_line = "[WITNESS] get_user calls -> a/x.py:1"
    out1 = b.trim(a_line, 500)
    b.commit_delivered(out1.pending_lines)
    out2 = b.trim("[WITNESS] get_user called by -> b/y.py:4", 500)
    assert out2.text != "", "a distinct fact sharing tag+symbol must survive"


# ============================================================================ #
# L-2 — traces.py extracts Python SyntaxError frames (no ``, in func`` clause)
# ============================================================================ #
def test_l2_syntaxerror_frame_is_extracted():
    from groundtruth.pretask.traces import _frames_from_text
    syn = ('  File "a/x.py", line 1\n'
           '    def get_user(:\n'
           '                 ^\n'
           'SyntaxError: invalid syntax')
    frames = _frames_from_text(syn)
    py = [f for f in frames if f.lang == "python"]
    assert len(py) == 1
    assert py[0].file == "a/x.py" and py[0].line == 1 and py[0].func == ""


def test_l2_normal_frame_not_double_matched():
    """The no-`in` pattern must NOT also fire on a normal `…, in func` frame — one
    frame, WITH its function name, and never a backtracked short-line-number twin."""
    from groundtruth.pretask.traces import _frames_from_text
    norm = ('Traceback (most recent call last):\n'
            '  File "patroni/postmaster.py", line 142, in activate\n'
            '    self._fd.write(b"x")\n')
    py = [f for f in _frames_from_text(norm) if f.lang == "python"]
    assert len(py) == 1
    assert py[0].line == 142 and py[0].func == "activate"


# ============================================================================ #
# O-2 — obligation FRESHNESS: a post-test edit demotes TESTED -> EDITED
# O-3 — status_vector_hash is 16 hex
# ============================================================================ #
class _View:
    def __init__(self, idx, verbatim, sym_parts):
        self.idx = idx
        self.verbatim = verbatim
        self.sym_parts = list(sym_parts)


def test_o2_stale_edit_after_test_demotes():
    from groundtruth.runtime.obligations import ObligationTracker, ObligationLifecycle
    t = ObligationTracker([_View(0, "get_user must validate", ["get_user"])])
    # turn 5: tested -> TESTED
    t.update(edited_tokens={"get_user"}, tested_tokens={"get_user"}, turn=5)
    assert t.obligations[0].status == ObligationLifecycle.TESTED.value
    # turn 7: the SAME symbol is edited again (per-turn delta) -> stale -> EDITED
    trans = t.apply_edit_freshness({"get_user"}, turn=7)
    assert trans == [(0, ObligationLifecycle.TESTED.value, ObligationLifecycle.EDITED.value)]
    assert t.obligations[0].status == ObligationLifecycle.EDITED.value


def test_o2_edit_not_newer_than_test_does_not_demote():
    from groundtruth.runtime.obligations import ObligationTracker, ObligationLifecycle
    t = ObligationTracker([_View(0, "get_user must validate", ["get_user"])])
    t.update(edited_tokens={"get_user"}, tested_tokens={"get_user"}, turn=5)
    # an edit at the SAME turn (not strictly later) is the pre-test edit -> stays fresh
    assert t.apply_edit_freshness({"get_user"}, turn=5) == []
    assert t.obligations[0].status == ObligationLifecycle.TESTED.value


def test_o2_unrelated_edit_does_not_demote():
    from groundtruth.runtime.obligations import ObligationTracker, ObligationLifecycle
    t = ObligationTracker([_View(0, "get_user must validate", ["get_user"])])
    t.update(edited_tokens={"get_user"}, tested_tokens={"get_user"}, turn=5)
    assert t.apply_edit_freshness({"other_symbol"}, turn=9) == []
    assert t.obligations[0].status == ObligationLifecycle.TESTED.value


def test_o3_status_vector_hash_is_16_hex():
    from groundtruth.runtime.obligations import status_vector_hash

    class _S:
        def __init__(self, idx):
            self.idx = idx
    statuses = [(_S(0), "tested", True, 1.0), (_S(1), "edited_untested", True, 0.5)]
    h = status_vector_hash(statuses)
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)


def test_o2_freshness_flag_ae_forwarded():
    wf = (_REPO / ".github/workflows/deepswe_full.yml").read_text(encoding="utf-8")
    ae = (_REPO / "artifact_deepswe/gt_integration/gt_ae_block.sh").read_text(encoding="utf-8")
    assert ("--ae GT_OBLIGATION_FRESHNESS=" in wf) or \
        ('--ae "GT_OBLIGATION_FRESHNESS=' in ae)


# ============================================================================ #
# S-1 — D7 consumption judge is relatedness-aware (flag GT_D7_RELATEDNESS)
# ============================================================================ #
import gt_mini_patch as _g  # noqa: E402


def test_s1_cmd_touches_target_generous():
    assert _g._cmd_touches_target("vim a/x.py", "a/x.py") is True      # exact path
    assert _g._cmd_touches_target("vim x.py", "a/x.py") is True        # basename
    assert _g._cmd_touches_target("vim b/y.py", "a/x.py") is False     # unrelated
    assert _g._cmd_touches_target("anything", "") is True              # unknown -> credit


def test_s1_default_off_is_relatedness_blind(monkeypatch):
    """Flag OFF: any acted() next-turn credits every delivered kind (historical)."""
    monkeypatch.delenv("GT_D7_RELATEDNESS", raising=False)
    _g._GT_BASELINE = False
    _g._reset_pending_delivery()
    _g._ledger_consumed_kinds.clear()
    _g._ledger_note_delivery("l3.contract", "cmd", "a/x.py")
    _g._ledger_judge_pending("sed -i 's/a/b/' b/other.py")  # edits an unrelated file
    assert "l3.contract" in _g._ledger_consumed_kinds  # blind credit (flag off)


def test_s1_flag_on_unrelated_edit_does_not_credit(monkeypatch):
    monkeypatch.setenv("GT_D7_RELATEDNESS", "1")
    _g._GT_BASELINE = False
    _g._reset_pending_delivery()
    _g._ledger_consumed_kinds.clear()
    _g._ledger_ignore_counts.clear()
    _g._ledger_note_delivery("l3.contract", "cmd", "a/x.py")
    _g._ledger_judge_pending("sed -i 's/a/b/' b/other.py")  # unrelated edit
    assert "l3.contract" not in _g._ledger_consumed_kinds  # no false consumed
    assert "l3.contract" not in _g._ledger_ignore_counts   # nor blamed as ignored


def test_s1_flag_on_related_edit_credits(monkeypatch):
    monkeypatch.setenv("GT_D7_RELATEDNESS", "1")
    _g._GT_BASELINE = False
    _g._reset_pending_delivery()
    _g._ledger_consumed_kinds.clear()
    _g._ledger_note_delivery("l3.contract", "cmd", "a/x.py")
    _g._ledger_judge_pending("sed -i 's/a/b/' a/x.py")   # edits the delivered target
    assert "l3.contract" in _g._ledger_consumed_kinds


def test_s1_flag_ae_forwarded():
    wf = (_REPO / ".github/workflows/deepswe_full.yml").read_text(encoding="utf-8")
    ae = (_REPO / "artifact_deepswe/gt_integration/gt_ae_block.sh").read_text(encoding="utf-8")
    assert ("--ae GT_D7_RELATEDNESS=" in wf) or ('--ae "GT_D7_RELATEDNESS=' in ae)


# ============================================================================ #
# SD-1 — semantic_drift is suppressed during an active repair cycle
# ============================================================================ #
def test_sd1_suppressed_when_last_test_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(_g, "_root", lambda: str(tmp_path))
    (tmp_path / "x.py").write_text("def f(x):\n    if x: return 1\n    return 0\n",
                                   encoding="utf-8")
    _g._sem_cache.clear()
    _g._sem_seed("x.py")  # baseline captured (has a guard + a return path)
    # edit removes the guard + a return path
    (tmp_path / "x.py").write_text("def f(x):\n    return 2\n", encoding="utf-8")
    # repair context: last observed test FAILED -> drift nudge suppressed
    _g._last_test_outcome_failed = True
    assert _g._semantic_drift_candidate("x.py") is None


def test_sd1_fires_when_tests_green(tmp_path, monkeypatch):
    monkeypatch.setattr(_g, "_root", lambda: str(tmp_path))
    (tmp_path / "x.py").write_text("def f(x):\n    if x: return 1\n    return 0\n",
                                   encoding="utf-8")
    _g._sem_cache.clear()
    _g._sem_seed("x.py")
    (tmp_path / "x.py").write_text("def f(x):\n    return 2\n", encoding="utf-8")
    _g._last_test_outcome_failed = False   # green/unknown -> the regression warning fires
    out = _g._semantic_drift_candidate("x.py")
    assert out is not None and "semantic_drift" in out[1]
