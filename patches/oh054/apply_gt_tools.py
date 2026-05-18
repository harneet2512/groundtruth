#!/usr/bin/env python3
"""Patch OH 0.54 CodeActAgent to register GT tools as native function-calling tools.

Run after OH is installed: python patches/oh054/apply_gt_tools.py

Patches:
1. Copies gt_tools.py into OH's tools directory
2. Adds GT tool registration to _get_tools() (gated by GT_REGISTER_TOOLS=1)
3. Adds GT tool dispatch to function_calling.py
"""
import importlib
import os
import shutil
import sys


def find_oh_path():
    try:
        import openhands.agenthub.codeact_agent.codeact_agent as m
        return os.path.dirname(m.__file__)
    except ImportError:
        for p in ["/tmp/OpenHands/openhands/agenthub/codeact_agent",
                  "/opt/OpenHands/openhands/agenthub/codeact_agent"]:
            if os.path.isdir(p):
                return p
    return None


def patch():
    oh_dir = find_oh_path()
    if not oh_dir:
        print("ERROR: Cannot find OH CodeActAgent directory")
        sys.exit(1)

    patch_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. Copy gt_tools.py
    src = os.path.join(patch_dir, "gt_tools.py")
    dst = os.path.join(oh_dir, "tools", "gt_tools.py")
    shutil.copy2(src, dst)
    print(f"Copied gt_tools.py -> {dst}")

    # 2. Patch codeact_agent.py: add GT tool registration
    agent_py = os.path.join(oh_dir, "codeact_agent.py")
    text = open(agent_py, encoding="utf-8").read()
    if "GT_REGISTER_TOOLS" not in text:
        marker = "        return tools\n\n    def reset"
        inject = (
            '        # GT tools: register gt_query and gt_validate as native tools\n'
            '        if os.environ.get("GT_REGISTER_TOOLS", "0") == "1":\n'
            '            try:\n'
            '                from openhands.agenthub.codeact_agent.tools.gt_tools import GtQueryTool, GtValidateTool\n'
            '                tools.append(GtQueryTool)\n'
            '                tools.append(GtValidateTool)\n'
            '            except ImportError:\n'
            '                pass\n'
            '        return tools\n\n    def reset'
        )
        if marker in text:
            text = text.replace(marker, inject)
            open(agent_py, "w", encoding="utf-8").write(text)
            print(f"Patched codeact_agent.py: GT tool registration added")
        else:
            print(f"WARN: Could not find marker in codeact_agent.py")
    else:
        print("codeact_agent.py already patched")

    # 3. Patch function_calling.py: add GT tool dispatch
    fc_py = os.path.join(oh_dir, "function_calling.py")
    fc_text = open(fc_py, encoding="utf-8").read()
    if "gt_query" not in fc_text:
        marker = "            # ================================================\n            # MCPAction (MCP)"
        inject = (
            "            # ================================================\n"
            "            # GT Tools (gt_query, gt_validate)\n"
            "            # ================================================\n"
            "            elif tool_call.function.name == 'gt_query':\n"
            "                symbol = arguments.get('symbol', '')\n"
            "                action = CmdRunAction(command=f'gt_query {symbol}', timeout=15)\n"
            "            elif tool_call.function.name == 'gt_validate':\n"
            "                file_path = arguments.get('file_path', '')\n"
            "                action = CmdRunAction(command=f'gt_validate {file_path}', timeout=15)\n"
            "\n"
            "            # ================================================\n"
            "            # MCPAction (MCP)"
        )
        if marker in fc_text:
            fc_text = fc_text.replace(marker, inject)
            open(fc_py, "w", encoding="utf-8").write(fc_text)
            print(f"Patched function_calling.py: GT tool dispatch added")
        else:
            print(f"WARN: Could not find marker in function_calling.py")
    else:
        print("function_calling.py already patched")

    print("GT tools patch complete. Set GT_REGISTER_TOOLS=1 to enable.")


if __name__ == "__main__":
    patch()
