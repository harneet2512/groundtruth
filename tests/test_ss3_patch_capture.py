"""SS-3 defect-3 — PATCH-CAPTURE binary exclusion.

Reproduces run-29236533134 / keras-team__keras-20396: the harness captured
``git diff HEAD`` after ``git add -A -N``, which swept the agent's repro-generated
binaries (``model.keras``, ``model.weights.h5``) into the diff as ``Binary files …
differ`` blocks. ``git apply`` rejects the whole patch → no report.json → false-fail.

The filter drops binary file-blocks (keying on git's OWN marker, not an extension list)
while keeping every text hunk, and ``choose_patch`` prefers the agent's curated patch
when it is applyable. Fixtures include the REAL keras diff shape.

Each test carries a biting mutation note.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PCF_PATH = os.path.join(_ROOT, "scripts", "swebench", "patch_capture_filter.py")
_spec = importlib.util.spec_from_file_location("patch_capture_filter_ss3", _PCF_PATH)
pcf = importlib.util.module_from_spec(_spec)
sys.modules["patch_capture_filter_ss3"] = pcf
_spec.loader.exec_module(pcf)


# The keras-20396 shape: two real text file-blocks + two binary file-blocks.
_KERAS_DIFF = """\
diff --git a/keras/src/backend/tensorflow/trainer.py b/keras/src/backend/tensorflow/trainer.py
index 1111111..2222222 100644
--- a/keras/src/backend/tensorflow/trainer.py
+++ b/keras/src/backend/tensorflow/trainer.py
@@ -10,6 +10,7 @@ class TensorFlowTrainer:
     def fit(self):
-        pass
+        self._maybe_build()
+        return None
diff --git a/keras/src/trainers/trainer.py b/keras/src/trainers/trainer.py
index 3333333..4444444 100644
--- a/keras/src/trainers/trainer.py
+++ b/keras/src/trainers/trainer.py
@@ -20,3 +20,4 @@ class Trainer:
     def compile(self):
-        pass
+        self._compiled = True
diff --git a/model.keras b/model.keras
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/model.keras differ
diff --git a/model.weights.h5 b/model.weights.h5
new file mode 100644
index 0000000..6666666
Binary files /dev/null and b/model.weights.h5 differ
"""


def test_binary_blocks_removed_text_hunks_kept():
    out = pcf.filter_binary_file_blocks(_KERAS_DIFF)
    assert "Binary files" not in out
    assert "diff --git a/model.keras" not in out
    assert "diff --git a/model.weights.h5" not in out
    # both source fixes survive intact.
    assert out.count("diff --git a/keras/src") == 2
    assert "self._maybe_build()" in out
    assert "self._compiled = True" in out
    # MUTATION: match "Binary files" as a naked substring (not line-start + " differ")
    #           → a text hunk that literally mentions "Binary files" would be dropped;
    #           conversely dropping the marker check keeps the un-applyable binary blocks.


def test_raw_is_unapplyable_filtered_is_applyable():
    assert pcf.looks_like_valid_patch(_KERAS_DIFF) is False   # binary blocks present
    assert pcf.looks_like_valid_patch(pcf.filter_binary_file_blocks(_KERAS_DIFF)) is True
    # MUTATION: make looks_like_valid_patch ignore residual binary blocks → raw would
    #           read as valid and the binary contamination would reach `git apply`.


def test_git_binary_patch_form_also_dropped():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/blob.bin b/blob.bin\n"
        "new file mode 100644\nGIT binary patch\nliteral 5\nHc$@<O00001\n"
    )
    out = pcf.filter_binary_file_blocks(diff)
    assert "GIT binary patch" not in out
    assert "diff --git a/blob.bin" not in out
    assert "diff --git a/a.py" in out


def test_text_hunk_mentioning_binary_files_is_preserved():
    # a legit source edit whose ADDED line contains the words "Binary files" must NOT be
    # dropped — the marker check is line-start + trailing " differ", not a substring.
    diff = (
        "diff --git a/doc.py b/doc.py\n"
        "--- a/doc.py\n+++ b/doc.py\n@@ -1 +1,2 @@\n"
        ' x = 1\n+# note: Binary files are handled elsewhere\n'
    )
    out = pcf.filter_binary_file_blocks(diff)
    assert "diff --git a/doc.py" in out
    assert "Binary files are handled elsewhere" in out


def test_choose_prefers_curated_patch_when_applyable(tmp_path):
    curated = tmp_path / "patch.txt"
    curated.write_text(
        "diff --git a/only.py b/only.py\n--- a/only.py\n+++ b/only.py\n"
        "@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8")
    gitdiff = tmp_path / "git_diff_head.diff"
    gitdiff.write_text(_KERAS_DIFF, encoding="utf-8")
    text, chosen = pcf.choose_patch([str(curated), str(gitdiff)])
    assert chosen == str(curated)
    assert "only.py" in text
    # MUTATION: reverse the priority (gitdiff first) → the keras binary diff would be
    #           chosen and (unfiltered form) fail apply.


def test_choose_falls_back_to_gitdiff_when_curated_missing_or_wrapped(tmp_path):
    # curated patch is PTY-wrapped/malformed (no valid hunk) → fall through to the
    # binary-filtered git diff (which IS applyable after filtering).
    curated = tmp_path / "patch.txt"
    curated.write_text("this is a wrapped, non-diff blob with no hunks\n", encoding="utf-8")
    gitdiff = tmp_path / "git_diff_head.diff"
    gitdiff.write_text(_KERAS_DIFF, encoding="utf-8")
    text, chosen = pcf.choose_patch([str(curated), str(gitdiff)])
    assert chosen == str(gitdiff)
    assert "Binary files" not in text
    assert text.count("diff --git a/keras/src") == 2


def test_cli_filter_mode(tmp_path):
    src = tmp_path / "in.diff"
    src.write_text(_KERAS_DIFF, encoding="utf-8")
    out = tmp_path / "out.diff"
    rc = pcf.main([str(src), str(out)])
    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert "Binary files" not in body
    assert body.count("diff --git a/keras/src") == 2


def test_cli_choose_mode(tmp_path):
    curated = tmp_path / "patch.txt"
    curated.write_text(
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8")
    out = tmp_path / "out.diff"
    rc = pcf.main(["--choose", str(out), str(curated), str(tmp_path / "missing.diff")])
    assert rc == 0
    assert "c.py" in out.read_text(encoding="utf-8")


def test_empty_and_none_are_safe():
    assert pcf.filter_binary_file_blocks("") == ""
    assert pcf.looks_like_valid_patch("") is False
    text, chosen = pcf.choose_patch([])
    assert text == "" and chosen is None
