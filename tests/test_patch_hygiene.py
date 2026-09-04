"""CP010 — patch hygiene classification tests."""

import importlib.util
import os

_PH_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "patch_hygiene.py")
_spec = importlib.util.spec_from_file_location("patch_hygiene_t", _PH_PATH)
ph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ph)


PATCH_SOURCE = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1 +1 @@
-x = 1
+x = 2
"""

PATCH_LOCKFILE = """diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-old
+new
"""


def test_classify_source_fix():
    r = ph.classify_patch(PATCH_SOURCE)
    assert r["classification"] == "source_fix"
    assert r["summary"]["source_fix"] == 1


def test_classify_lockfile_only():
    r = ph.classify_patch(PATCH_LOCKFILE)
    assert r["classification"] == "lockfile"


def test_missing_patch():
    r = ph.classify_patch("")
    assert r["classification"] == "missing_model_patch"
