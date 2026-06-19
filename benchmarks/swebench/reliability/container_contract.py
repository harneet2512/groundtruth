"""CONTAINER-RUNTIME contract — GT actually ran inside the eval container.

Runs INSIDE the eval container (invoked by emit_incontainer) and records the
facts that prove GT's runtime executed in-container off the baked /opt/gt
closure, not on the host or from a checkout: import path, flags, baked model,
graph-built-here, no host copy for LSP/embedder. Read-only.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys

_REQUIRED_FLAGS = (
    "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER", "GT_FORCE_ONNX_EMBEDDER",
    "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK", "GT_FORBID_PREBUILT_GRAPH",
)


def build_container_contract(task_id: str, repo_root: str, graph_db: str) -> dict:
    c: dict = {"contract": "container_runtime", "task_id": task_id}
    c["hostname"] = socket.gethostname()
    c["container_marker"] = os.path.exists("/.dockerenv")
    c["opt_gt_exists"] = os.path.isdir("/opt/gt")
    c["source_root"] = repo_root
    c["source_root_exists"] = bool(repo_root and os.path.isdir(repo_root))
    c["graph_db_path"] = graph_db
    c["graph_db_present"] = bool(graph_db and os.path.exists(graph_db))
    # graph built in-container: it lives under the in-container out dir, not copied in
    c["graph_built_in_container"] = bool(graph_db and graph_db.startswith("/tmp/gt") and os.path.exists(graph_db))

    # the import path Python actually uses for groundtruth (must be /opt/gt/src)
    try:
        import groundtruth as _g
        c["groundtruth_import_path"] = getattr(_g, "__file__", "")
        c["import_from_opt_gt"] = "/opt/gt/" in (getattr(_g, "__file__", "") or "")
    except Exception as e:
        c["groundtruth_import_path"] = f"ERR({e})"
        c["import_from_opt_gt"] = False

    # toolchain presence in-container
    c["python_version"] = sys.version.split()[0]
    c["python_executable"] = sys.executable
    c["python_from_opt_gt"] = "/opt/gt/" in sys.executable
    c["gt_index_bin"] = shutil.which("gt-index") or "/opt/gt/bin/gt-index"
    c["gt_index_present"] = os.path.exists(c["gt_index_bin"])
    c["pyright_available"] = bool(shutil.which("pyright"))
    c["node_available"] = bool(shutil.which("node"))
    try:
        import onnxruntime as _ort
        c["onnxruntime_import_ok"] = True
        c["onnxruntime_version"] = getattr(_ort, "__version__", "?")
    except Exception as e:
        c["onnxruntime_import_ok"] = False
        c["onnxruntime_error"] = str(e)

    # baked model
    root = os.environ.get("GT_MODELS_ROOT", "")
    c["GT_MODELS_ROOT"] = root
    onnx = os.path.join(root, "e5-small-v2", "model.onnx") if root else ""
    c["model_baked"] = bool(onnx and os.path.exists(onnx))

    # the fail-closed flags as actually seen by this process
    c["flags"] = {f: os.environ.get(f, "") for f in _REQUIRED_FLAGS}
    c["flags_all_set"] = all(os.environ.get(f) == "1" for f in _REQUIRED_FLAGS)

    hf: list[str] = []
    if not c["opt_gt_exists"]:
        hf.append("opt_gt_absent")
    if not c["import_from_opt_gt"]:
        hf.append("groundtruth_imported_from_non_opt_gt")  # imported from checkout/host
    if not c["graph_built_in_container"]:
        hf.append("graph_not_built_in_container")
    if not c["model_baked"]:
        hf.append("model_not_baked")
    if not c["flags_all_set"]:
        hf.append("required_flags_not_all_set")
    c["hard_fail"] = hf
    return c
