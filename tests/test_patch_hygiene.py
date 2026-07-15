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


def test_python_stub_is_source_fix():
    assert ph.classify_file("src/package/api.pyi") == "source_fix"


def test_executable_yaml_is_source_but_documentation_yaml_is_noise():
    workflow = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,3 +1,3 @@
 jobs:
   test:
-    runs-on: ubuntu-22.04
+    runs-on: ubuntu-24.04
"""
    manifest = """diff --git a/deploy/service.yaml b/deploy/service.yaml
--- a/deploy/service.yaml
+++ b/deploy/service.yaml
@@ -1,2 +1,2 @@
 apiVersion: apps/v1
-kind: Deployment
+kind: StatefulSet
"""
    docs = """diff --git a/docs/example.yml b/docs/example.yml
--- a/docs/example.yml
+++ b/docs/example.yml
@@ -1 +1 @@
-color: blue
+color: green
"""

    assert ph.classify_patch(workflow)["classification"] == "source_fix"
    assert ph.classify_patch(manifest)["classification"] == "source_fix"
    assert ph.classify_patch(docs)["classification"] == "noise"


def test_policy_dsl_yaml_is_source_but_expected_fixture_is_not():
    policy = """diff --git a/checkov/terraform/checks/graph_checks/AzureSubnetConfigWithNSG.yaml b/checkov/terraform/checks/graph_checks/AzureSubnetConfigWithNSG.yaml
--- a/checkov/terraform/checks/graph_checks/AzureSubnetConfigWithNSG.yaml
+++ b/checkov/terraform/checks/graph_checks/AzureSubnetConfigWithNSG.yaml
@@ -1,4 +1,4 @@
 definition:
   cond_type: attribute
-  operator: equals
+  operator: not_equals
   resource_types: [subnet]
"""
    expected_fixture = """diff --git a/tests/fixtures/expected.yaml b/tests/fixtures/expected.yaml
--- a/tests/fixtures/expected.yaml
+++ b/tests/fixtures/expected.yaml
@@ -1,2 +1,2 @@
 apiVersion: apps/v1
-kind: Deployment
+kind: StatefulSet
"""

    assert ph.classify_patch(policy)["classification"] == "source_fix"
    assert ph.classify_patch(expected_fixture)["classification"] == "noise"
