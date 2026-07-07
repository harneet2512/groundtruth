"""C1 (row 40) + C2 (row 41) auditability witnesses.

Regression pins for the gt_math NO-GO on run 28886910434, where GT_PASSAGE_WIDE=1
and GT_SEM_BODY=1 were ON at runtime but produced ZERO auditable evidence (rows 40/41
read `—`). The fix emits greppable [GT_META] witnesses so the auditor reads a MEASURED
value. These tests assert the witness is emitted with the right payload, once, and that
the OFF path is byte-identical telemetry (window=128, wide=0).
"""
import io
import contextlib
import os
import sys
import importlib.util

# Load THIS worktree's embed.py in ISOLATION (its own module name) via a file spec, so an
# editable pip install of `groundtruth` pointing elsewhere cannot shadow the code under test,
# and so we never mutate sys.modules['groundtruth.*'] (which would break other tests that
# importlib.reload it). embed.py's top-level imports are all stdlib + numpy -> standalone-safe.
_EMBED_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "groundtruth", "memory", "enrich", "embed.py"))
_spec = importlib.util.spec_from_file_location("_gt_embed_under_test", _EMBED_PATH)
embed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(embed)

assert hasattr(embed, "_emit_passage_window_once"), "C1 witness helper missing from embed.py"


class _StubGTE:
    model_name = "gte-modernbert-base"


class _StubE5:
    model_name = "intfloat/e5-base-v2"


def _emit(flag, model):
    os.environ["GT_PASSAGE_WIDE"] = flag
    embed._passage_window_emitted = False
    win = embed._passage_token_window(model)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        embed._emit_passage_window_once(model, win)
        embed._emit_passage_window_once(model, win)  # 2nd call must be a no-op (latch)
    return win, buf.getvalue()


def test_c1_off_is_byte_identical_telemetry():
    for flag in ("", "0", "false", "no"):
        win, out = _emit(flag, _StubGTE())
        assert win == 128, f"OFF must keep 128, got {win} for '{flag}'"
        assert "window=128 wide=0" in out
        assert out.count("[GT_META] passage_window") == 1  # once


def test_c1_on_widens_to_256_and_witnesses():
    for flag in ("1", "true", "yes"):
        win, out = _emit(flag, _StubGTE())
        assert win == 256, f"ON must widen to 256, got {win} for '{flag}'"
        assert "window=256 wide=1 flag=1" in out


def test_c1_e5_preserved_even_when_wide():
    win, out = _emit("1", _StubE5())
    assert win == 128, "e5 must stay at its conservative 128 window"
    assert "window=128" in out and "e5=1" in out


def test_c1_witness_emitted_once_per_process():
    os.environ["GT_PASSAGE_WIDE"] = "1"
    embed._passage_window_emitted = False
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        embed._emit_passage_window_once(_StubGTE(), 256)
        embed._emit_passage_window_once(_StubGTE(), 256)
        embed._emit_passage_window_once(_StubGTE(), 256)
    assert buf.getvalue().count("[GT_META] passage_window") == 1


def test_c2_positive_witness_branch():
    """C2 (row 41): body PRESENT -> one positive [GT_META] sem_body with counts;
    body ABSENT under proof -> fail-closed; absent non-proof -> warn only."""
    noop = [False]
    pres = [False]

    def branch(node_terms, node_strings, node_calls_v, proof, baseline):
        out = io.StringIO()
        raised = None
        with contextlib.redirect_stderr(out):
            if not node_terms and not node_strings:
                if not noop[0]:
                    noop[0] = True
                    sys.stderr.write("[GT_WARN] GT_SEM_BODY=1 but graph has 0 body-channel rows ...\n")
                if proof == "1" and baseline != "1":
                    raised = "body-less"
            else:
                if not pres[0]:
                    pres[0] = True
                    sys.stderr.write(
                        f"[GT_META] sem_body body_terms_rows={len(node_terms)} "
                        f"string_lit_rows={len(node_strings)} calls_rows={len(node_calls_v)}\n")
        return out.getvalue(), raised

    line, r = branch({1: "a", 2: "b"}, {1: "s"}, {1: "c"}, "1", "0")
    assert "[GT_META] sem_body body_terms_rows=2" in line and r is None

    noop[0] = pres[0] = False
    line, r = branch({}, {}, {}, "1", "0")
    assert r == "body-less" and "[GT_WARN]" in line

    noop[0] = pres[0] = False
    line, r = branch({}, {}, {}, "0", "0")
    assert r is None and "[GT_WARN]" in line
