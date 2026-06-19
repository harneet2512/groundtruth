#!/usr/bin/env python3
"""Patch run_infer.py to wrap runtime.run_action for GT evidence delivery.

After initialize_runtime injects gt_hook.py, this wraps runtime.run_action
so that after every file-editing action, GT evidence is appended to the
observation the agent sees.

This is the DELIVERY mechanism. The watcher/extraction is TELEMETRY.
"""
import sys
import py_compile

MARKER = "# >>> GT_RUNTIME_WRAPPER"

WRAPPER_CODE = '''
    # >>> GT_RUNTIME_WRAPPER: wrap runtime.run_action for evidence delivery
    if _gt_hook_path and os.path.isfile(_gt_hook_path):
        _orig_run_action = runtime.run_action
        _gt_call_count = [0]

        def _gt_wrapped_run_action(action):
            obs = _orig_run_action(action)
            try:
                action_type = getattr(action, 'action', '')
                is_edit = action_type in ('edit', 'write')
                is_cmd_edit = False
                if action_type == 'run':
                    cmd = getattr(action, 'command', '')
                    is_cmd_edit = any(p in cmd for p in [
                        'str_replace', 'sed -i', 'sed -e', "cat >", "cat >>",
                        "echo >", "echo >>", "tee ", "patch ", ">>> ",
                    ])
                if is_edit or is_cmd_edit:
                    _gt_call_count[0] += 1
                    gt_act = CmdRunAction(
                        command="cd $(git -C /workspace/*/ rev-parse --show-toplevel 2>/dev/null || echo /workspace/*/) && "
                                "python3 /tmp/gt_hook.py verify --root=. --quiet --max-items=3 2>/dev/null || true"
                    )
                    gt_act.set_hard_timeout(35 if _gt_call_count[0] == 1 else 8)
                    gt_obs = _orig_run_action(gt_act)
                    gt_out = getattr(gt_obs, 'content', '')
                    if gt_out and len(gt_out.strip()) > 20 and 'GT' in gt_out:
                        obs_content = getattr(obs, 'content', '')
                        obs.content = obs_content + "\\n\\n" + gt_out.strip()
            except Exception:
                pass
            return obs

        runtime.run_action = _gt_wrapped_run_action
        logger.info("GT runtime wrapper installed — evidence will appear in observations")
    # <<< GT_RUNTIME_WRAPPER
'''


def main():
    if len(sys.argv) < 2:
        print("Usage: python oh_patch_runtime_wrapper.py /path/to/run_infer.py")
        return 1

    fpath = sys.argv[1]
    with open(fpath) as f:
        src = f.read()

    if MARKER in src:
        print("SKIP: runtime wrapper already applied")
        return 0

    # Insert after the GT_HOOK_INJECT initialize_runtime block
    end_marker = "# <<< GT_HOOK_INJECT: initialize_runtime"
    if end_marker not in src:
        print("ERROR: GT hook injection not found — apply oh_apply_patch.py first")
        return 1

    src = src.replace(end_marker, end_marker + "\n" + WRAPPER_CODE)

    with open(fpath, "w") as f:
        f.write(src)

    py_compile.compile(fpath, doraise=True)
    print("PATCHED: runtime wrapper installed after GT hook injection")
    print("  Evidence will appear in agent observations after file edits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
