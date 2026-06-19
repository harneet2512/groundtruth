#!/usr/bin/env python3
"""Idempotent patch: insert GT_PATCH_V4 (kernel hook) into run_infer.py.

Inserts after the GT_PATCH_V2 closing marker. Block:
  - reads GT_KERNEL_HOOK_PATH env var (default off)
  - chunk-injects that file as /tmp/gt_kernel_check.py
  - wraps runtime.run_action a second time so the kernel check fires after
    each edit and its output appends to the observation the agent sees

Idempotent: if GT_PATCH_V4 marker already in the file, exits 0 with no change.
"""
from __future__ import annotations

import sys
from pathlib import Path

V4_MARKER = "# >>> GT_PATCH_V4: kernel hook"
V2_END_MARKER = "# <<< GT_PATCH_V2: initialize_runtime"

V4_BLOCK = '''
    # >>> GT_PATCH_V4: kernel hook
    _gt_kernel_path = os.environ.get("GT_KERNEL_HOOK_PATH", "")
    if _gt_kernel_path and os.path.isfile(_gt_kernel_path):
        try:
            import base64 as _b64v4
            with open(_gt_kernel_path, "rb") as _fh:
                _kernel_b64 = _b64v4.b64encode(_fh.read()).decode("ascii")
            _CHUNK = 8000
            _kchunks = [_kernel_b64[i:i+_CHUNK] for i in range(0, len(_kernel_b64), _CHUNK)]
            _kok = True
            for _i, _chunk in enumerate(_kchunks):
                _op = ">" if _i == 0 else ">>"
                _act = CmdRunAction(command="echo -n '" + _chunk + "' " + _op + " /tmp/gt_kernel.b64")
                _act.set_hard_timeout(30)
                _obs = runtime.run_action(_act)
                if not isinstance(_obs, CmdOutputObservation) or _obs.exit_code != 0:
                    _kok = False
                    break
            if _kok:
                _act = CmdRunAction(command="base64 -d /tmp/gt_kernel.b64 > /tmp/gt_kernel_check.py && chmod +x /tmp/gt_kernel_check.py && rm -f /tmp/gt_kernel.b64 && rm -f /tmp/gt_edits.jsonl && echo GT_KERNEL_READY")
                _act.set_hard_timeout(30)
                _obs = runtime.run_action(_act)
                if "GT_KERNEL_READY" in getattr(_obs, "content", ""):
                    logger.info("GT kernel hook injected: %s", instance.instance_id)

                    # Wrap run_action ONCE MORE to chain kernel check after edits
                    _orig_run_v4 = runtime.run_action
                    def _gt_kernel_wrap(action, _orig=_orig_run_v4):
                        obs = _orig(action)
                        try:
                            _raw = getattr(action, "action", "")
                            atype = getattr(_raw, "value", str(_raw))
                            fpath = getattr(action, "path", "")
                            if atype in ("edit", "write") and fpath:
                                if "/workspace/" in fpath:
                                    parts = fpath.split("/workspace/", 1)[1]
                                    rel = parts.split("/", 1)[1] if "/" in parts else parts
                                else:
                                    rel = fpath
                                ws_root = "/workspace/$(ls -d /workspace/*/ 2>/dev/null | head -1 | xargs -n1 basename)"
                                kcmd = (
                                    "python3 /tmp/gt_kernel_check.py "
                                    "--edit-path '" + fpath + "' "
                                    "--brief-jsonl /tmp/gt_pretask.jsonl "
                                    "--workspace-root " + ws_root + " "
                                    "--edit-history /tmp/gt_edits.jsonl "
                                    "2>/dev/null || true"
                                )
                                ka = CmdRunAction(command=kcmd)
                                ka.set_hard_timeout(15)
                                ko = _orig(ka)
                                kout = getattr(ko, "content", "") or ""
                                if "<gt-kernel-decision>" in kout:
                                    existing = getattr(obs, "content", "") or ""
                                    obs.content = existing + "\\n\\n" + kout.strip()
                                    logger.info("GT_KERNEL: decision appended for %s", rel)
                        except Exception as _ke:
                            logger.warning("GT_KERNEL: wrap error %s", _ke)
                        return obs
                    runtime.run_action = _gt_kernel_wrap
                    logger.info("GT kernel runtime wrapper installed")
        except Exception as _ke2:
            logger.warning("GT kernel injection error: %s", _ke2)
    # <<< GT_PATCH_V4: kernel hook
'''


def main(target: str = "/home/Lenovo/oh-benchmarks/evaluation/benchmarks/swe_bench/run_infer.py") -> int:
    p = Path(target)
    src = p.read_text(encoding="utf-8")

    if V4_MARKER in src:
        print(f"already patched (V4 marker present in {p})")
        return 0

    if V2_END_MARKER not in src:
        print(f"ERROR: V2 end marker '{V2_END_MARKER}' not found in {p}")
        return 1

    new_src = src.replace(V2_END_MARKER, V2_END_MARKER + V4_BLOCK)
    bak = p.with_suffix(p.suffix + ".bak_pre_v4")
    if not bak.exists():
        bak.write_text(src, encoding="utf-8")
        print(f"backup written: {bak}")
    p.write_text(new_src, encoding="utf-8")
    print(f"V4 patch applied to {p} (+{len(V4_BLOCK)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]) if len(sys.argv) > 1 else main())
