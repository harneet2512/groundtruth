"""GTRuntimeContext — single source of truth for GT's benchmark/proof runtime.

ONE context every GT benchmark entry point consults, so components stop independently
discovering host paths and can never silently fall back in proof mode. It:
  * resolves the canonical paths (runtime_root, source_root, graph_db, models_root,
    lsp_root, audit_dir) from env + container detection,
  * `export_env()` emits the canonical env (the vars components already read) + the
    8 proof flags, so everything downstream inherits one set of paths,
  * `validate()` fail-fasts under GT_PROOF_MODE=1 on the runtime/env/embedder/path
    contract (the dimension checks themselves stay in preflight_pipeline.py — reused).

It does NOT change ranking/scoring/brief logic. It only enforces the runtime contract.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
from dataclasses import dataclass

# Single proof-mode exception type for the whole runtime (defined in the leaf
# module so resolve.py / pretask / gates all raise+catch the SAME class).
from groundtruth.runtime import proof as _proof
from groundtruth.runtime.proof import GTProofModeError

# The proof-mode flag set. Present-and-"1" is asserted in proof mode.
REQUIRED_FLAGS = (
    "GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER",
    "GT_FORCE_ONNX_EMBEDDER", "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK",
    "GT_FORBID_PREBUILT_GRAPH",
)
_DEFAULT_RUNTIME_ROOT = "/opt/gt"


def _in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as f:
            data = f.read()
        return any(t in data for t in ("docker", "containerd", "kubepods", "/docker/"))
    except Exception:
        return False


@dataclass
class GTRuntimeContext:
    runtime_root: str
    source_root: str
    graph_db: str
    models_root: str
    lsp_root: str
    audit_dir: str
    inside_container: bool
    proof_mode: bool
    containerized: bool

    # ---- construction ----
    @classmethod
    def from_env(cls, source_root: str | None = None, graph_db: str | None = None,
                 audit_dir: str | None = None) -> "GTRuntimeContext":
        runtime_root = os.environ.get("GT_HOME") or _DEFAULT_RUNTIME_ROOT
        # In proof mode the canonical in-container paths are mandatory — the
        # GT_HOST_* aliases (a split host/container root) are rejected, never used
        # as a silent fallback (plan finding A1).
        if os.environ.get("GT_PROOF_MODE") == "1":
            _proof.reject_host_aliases()
            src = source_root or os.environ.get("GT_SOURCE_ROOT") or ""
            gdb = graph_db or os.environ.get("GT_GRAPH_DB") or ""
        else:
            src = source_root or os.environ.get("GT_SOURCE_ROOT") or os.environ.get("GT_HOST_SRC_ROOT") or ""
            gdb = graph_db or os.environ.get("GT_GRAPH_DB") or os.environ.get("GT_HOST_GRAPH_DB") or ""
        models = os.environ.get("GT_MODELS_ROOT") or os.path.join(runtime_root, "models")
        return cls(
            runtime_root=runtime_root,
            source_root=src,
            graph_db=gdb,
            models_root=models,
            lsp_root=src,  # LSP runs against the same source root, by construction
            audit_dir=audit_dir or os.environ.get("GT_AUDIT_DIR") or "",
            inside_container=_in_container(),
            proof_mode=os.environ.get("GT_PROOF_MODE") == "1",
            containerized=os.environ.get("GT_CONTAINERIZED") == "1",
        )

    # ---- the canonical env everything inherits ----
    def export_env(self) -> dict[str, str]:
        env = {
            "GT_HOME": self.runtime_root,
            "GT_SOURCE_ROOT": self.source_root,
            "GT_GRAPH_DB": self.graph_db,
            "GT_MODELS_ROOT": self.models_root,
            "GT_AUDIT_DIR": self.audit_dir,
            # Stable id of THIS runtime — stamped into graph meta, brief/gate
            # results and the run contract so gates-only and live prove identical.
            "GT_CONTEXT_ID": _proof.context_id(),
        }
        # in proof mode, assert all flags ON so no component silently degrades
        if self.proof_mode:
            for f in REQUIRED_FLAGS:
                env.setdefault(f, "1")
        return {k: v for k, v in env.items() if v}

    def apply_env(self) -> None:
        for k, v in self.export_env().items():
            os.environ[k] = v

    # ---- the runtime/env/embedder/path contract (dimensions live in preflight) ----
    def checks(self, require_graph: bool = False) -> list[tuple[str, bool, str]]:
        r: list[tuple[str, bool, str]] = []

        # proof flags present
        missing = [f for f in REQUIRED_FLAGS if os.environ.get(f) != "1"]
        r.append(("proof_flags_all_set", not missing, f"missing/not-1: {missing}" if missing else "all 8 = 1"))

        # inside the eval container
        r.append(("inside_container", self.inside_container,
                  "/.dockerenv + cgroup say host" if not self.inside_container else "container"))

        # groundtruth imported from under runtime_root (not a checkout/host path)
        try:
            import groundtruth as _g
            gf = getattr(_g, "__file__", "") or ""
            ok = self.runtime_root.rstrip("/") + "/" in gf or gf.startswith(self.runtime_root)
            r.append(("import_under_runtime_root", ok, gf))
        except Exception as e:
            r.append(("import_under_runtime_root", False, f"import error: {e}"))

        # source root present (in container)
        r.append(("source_root_exists", bool(self.source_root and os.path.isdir(self.source_root)),
                  self.source_root or "(unset)"))

        # baked model files present -> no runtime download
        onnx = os.path.join(self.models_root, "e5-small-v2", "model.onnx")
        tok = os.path.join(self.models_root, "e5-small-v2", "tokenizer.json")
        r.append(("model_files_baked", os.path.exists(onnx) and os.path.exists(tok), onnx))

        # onnxruntime importable
        try:
            import onnxruntime  # noqa: F401
            r.append(("onnxruntime_importable", True, getattr(onnxruntime, "__version__", "?")))
        except Exception as e:
            r.append(("onnxruntime_importable", False, str(e)))

        # ONNX forced (guarantees no sentence-transformers in either half)
        r.append(("force_onnx_set", os.environ.get("GT_FORCE_ONNX_EMBEDDER") == "1",
                  os.environ.get("GT_FORCE_ONNX_EMBEDDER", "")))

        # embedder is real (not Zero) and discriminates related>unrelated
        r.append(self._embedder_check())

        # an LSP server is launchable (pyright/node for python; others by ext at resolve time)
        r.append(("lsp_server_available", bool(shutil.which("pyright") and shutil.which("node")),
                  f"pyright={shutil.which('pyright')} node={shutil.which('node')}"))

        if require_graph:
            present = bool(self.graph_db and os.path.exists(self.graph_db))
            r.append(("graph_db_present", present, self.graph_db or "(unset)"))
            # built in container == not a prebuilt host path injected
            prebuilt = os.environ.get("GT_PREBUILT_GRAPH_DB", "")
            forbid = os.environ.get("GT_FORBID_PREBUILT_GRAPH") == "1"
            r.append(("not_prebuilt_when_forbidden", not (forbid and prebuilt),
                      f"forbid={forbid} prebuilt={prebuilt or 'none'}"))
        return r

    def _embedder_check(self) -> tuple[str, bool, str]:
        try:
            from groundtruth.memory.enrich.embed import get_embedding_model

            m = get_embedding_model()
            cls = type(m).__name__
            if "Zero" in cls:
                return ("embedder_real_not_zero", False, f"_ZeroEmbeddingModel ({cls})")

            def emb(t, q):
                return list(m.embed_batch([t], is_query=q)[0])

            def cos(x, y):
                d = sum(i * j for i, j in zip(x, y))
                nx = math.sqrt(sum(i * i for i in x)); ny = math.sqrt(sum(i * i for i in y))
                return d / (nx * ny) if nx and ny else 0.0

            a = emb("read configuration from a file", True)
            rel, unrel = emb("parse config settings from disk", False), emb("determinant of a matrix", False)
            sim, dis = cos(a, rel), cos(a, unrel)
            ok = bool(a) and all(math.isfinite(v) for v in a) and sim > dis and sim > 0.0
            return ("embedder_real_not_zero", ok, f"class={cls} cos_rel={sim:.4f} cos_unrel={dis:.4f}")
        except Exception as e:
            return ("embedder_real_not_zero", False, f"load error: {e}")

    def validate(self, require_graph: bool = False, raise_on_fail: bool | None = None) -> list[tuple[str, bool, str]]:
        results = self.checks(require_graph)
        do_raise = self.proof_mode if raise_on_fail is None else raise_on_fail
        if do_raise and any(not ok for _, ok, _ in results):
            raise GTProofModeError(results)
        return results

    def as_dict(self) -> dict:
        return {
            "runtime_root": self.runtime_root, "source_root": self.source_root,
            "graph_db": self.graph_db, "models_root": self.models_root,
            "lsp_root": self.lsp_root, "audit_dir": self.audit_dir,
            "inside_container": self.inside_container, "proof_mode": self.proof_mode,
            "containerized": self.containerized, "context_id": _proof.context_id(),
        }


def _main(argv: list[str]) -> int:
    """CLI: --export prints `export K=V` lines; --json prints the context;
    --validate runs the checks (require_graph if --graph given) and exits non-zero
    on any failure when proof_mode (or --strict)."""
    import json

    src = graph = audit = None
    export = jsonout = validate = strict = require_graph = False
    for a in argv:
        if a == "--export":
            export = True
        elif a == "--json":
            jsonout = True
        elif a == "--validate":
            validate = True
        elif a == "--strict":
            strict = True
        elif a == "--graph":
            require_graph = True
        elif a.startswith("--source="):
            src = a.split("=", 1)[1]
        elif a.startswith("--graph-db="):
            graph = a.split("=", 1)[1]
        elif a.startswith("--audit="):
            audit = a.split("=", 1)[1]

    ctx = GTRuntimeContext.from_env(source_root=src, graph_db=graph, audit_dir=audit)
    if export:
        for k, v in ctx.export_env().items():
            print(f"export {k}={v}")
        return 0
    if jsonout:
        print(json.dumps(ctx.as_dict(), indent=2))
        return 0
    if validate:
        try:
            results = ctx.validate(require_graph=require_graph,
                                   raise_on_fail=(strict or ctx.proof_mode))
        except GTProofModeError as e:
            for n, ok, d in e.failures:
                print(f"  {'ok  ' if ok else 'FAIL'} {n}: {d}")
            print("GT_PROOF_MODE: runtime contract FAILED")
            return 1
        for n, ok, d in results:
            print(f"  {'ok  ' if ok else 'WARN'} {n}: {d}")
        print("runtime contract OK")
        return 0
    print(json.dumps(ctx.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
