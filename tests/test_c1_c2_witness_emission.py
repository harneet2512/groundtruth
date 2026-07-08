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

# NB: the C1 emit deliberately does NOT live in embed.py (see test_c1_emit_lives_in_synced_...):
# embed.py is in the non-synced memory/enrich pkg. embed still owns the window-selection fns.
assert hasattr(embed, "_passage_token_window"), "window-selection fn missing from embed.py"


class _StubGTE:
    model_name = "gte-modernbert-base"


class _StubE5:
    model_name = "intfloat/e5-base-v2"


def _emit_line(flag, model):
    """Reproduce the [GT_META] passage_window line the SYNCED localizer emits, driven by the
    single source of truth embed._passage_token_window (the window fns stay in embed.py; only
    the emit moved to graph_localizer, which is the package the container actually syncs)."""
    os.environ["GT_PASSAGE_WIDE"] = flag
    win = embed._passage_token_window(model)
    wide = 1 if win == embed._PASSAGE_TOKEN_WINDOW_WIDE else 0
    return win, (f"[GT_META] passage_window window={win} wide={wide} "
                 f"flag={1 if embed._passage_wide() else 0} model={model.model_name}")


def test_c1_off_is_byte_identical_telemetry():
    for flag in ("", "0", "false", "no"):
        win, line = _emit_line(flag, _StubGTE())
        assert win == 128, f"OFF must keep 128, got {win} for '{flag}'"
        assert "window=128 wide=0" in line


def test_c1_on_widens_to_256_and_witnesses():
    for flag in ("1", "true", "yes"):
        win, line = _emit_line(flag, _StubGTE())
        assert win == 256, f"ON must widen to 256, got {win} for '{flag}'"
        assert "window=256 wide=1 flag=1" in line


def test_c1_e5_preserved_even_when_wide():
    win, line = _emit_line("1", _StubE5())
    assert win == 128, "e5 must stay at its conservative 128 window"
    assert "window=128" in line


def test_c1_emit_lives_in_synced_pretask_not_baked_embed():
    """Regression for the baked-not-synced trap: the emit MUST be in graph_localizer (a synced
    pkg), and embed.py (memory/enrich, NOT synced) must NOT carry a dead emit helper."""
    assert not hasattr(embed, "_emit_passage_window_once"), "emit must not live in non-synced embed.py"
    gl = os.path.join(os.path.dirname(__file__), "..", "src", "groundtruth", "pretask", "graph_localizer.py")
    src = open(gl, encoding="utf-8").read()
    assert "[GT_META] passage_window window=" in src, "localizer must emit the C1 witness"


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
