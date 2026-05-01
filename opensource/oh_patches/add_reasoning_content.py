"""Idempotent patch: add reasoning_content field to OH Message class.

Required for MiMo (and other Qwen3-thinking-style models) which emit a
reasoning_content field alongside tool_calls and require it to be
preserved across multi-turn conversations.

Per Xiaomi MiMo docs:
  "In thinking mode with multi-turn tool calls, the model returns a
  reasoning_content field alongside tool_calls. Users must persist all
  history reasoning_content in subsequent message arrays."

Usage on VM:
  python3 /home/Lenovo/opensource/oh_patches/add_reasoning_content.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_MSG = '/home/Lenovo/oh-benchmarks/openhands/core/message.py'

FIELD_LINE = '    reasoning_content: str | None = None  # for MiMo / Qwen3-thinking models\n'
FIELD_ANCHOR = '    force_string_serializer: bool = False\n'

SERIALIZER_INSERT = '''
        # MiMo / Qwen3-thinking: persist reasoning_content across turns
        if self.reasoning_content is not None:
            message_dict['reasoning_content'] = self.reasoning_content
'''
SERIALIZER_ANCHOR = '''        return message_dict
'''


def main(target: str = DEFAULT_MSG) -> int:
    p = Path(target)
    src = p.read_text(encoding='utf-8')

    if 'reasoning_content' in src:
        print(f'already patched: {p}')
        return 0

    # 1. Add field declaration
    if FIELD_ANCHOR not in src:
        print(f'ERROR: anchor "force_string_serializer: bool = False" not found in {p}')
        return 1
    new_src = src.replace(FIELD_ANCHOR, FIELD_ANCHOR + FIELD_LINE)

    # 2. Add serializer logic: insert before the FINAL `return message_dict` in _add_tool_call_keys
    # _add_tool_call_keys returns once at end. Insert just before that return.
    new_src = re.sub(
        r'(\n        # an observation message with tool response\n.*?\n            message_dict\[.name.\] = self\.name\n)(\n        return message_dict\n)',
        r'\1' + SERIALIZER_INSERT + r'\2',
        new_src,
        count=1,
        flags=re.DOTALL,
    )

    if 'reasoning_content' not in new_src:
        print('ERROR: serializer insert failed')
        return 1

    bak = p.with_suffix(p.suffix + '.bak_pre_reasoning_content')
    if not bak.exists():
        bak.write_text(src, encoding='utf-8')
        print(f'backup: {bak}')
    p.write_text(new_src, encoding='utf-8')
    print(f'patched: {p} (+{len(new_src) - len(src)} chars)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(*sys.argv[1:]) if len(sys.argv) > 1 else main())
