"""Build the self-contained V1R pretask bundle.

Same pattern as build_v7_bundle.py: zip groundtruth/pretask/*.py + runtime/*.py,
base64-encode, embed in a single Python launcher.

The launcher calls generate_v1r_brief() instead of generate_brief().
Output: no prose, no constraints — just ranked files + functions + test mappings.
"""
from __future__ import annotations

import base64
import io
import textwrap
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "groundtruth"
OUT = REPO_ROOT / "scripts" / "swebench" / "gt_pretask_brief_v1r.py"

PRETASK_FILES = sorted(p.name for p in (SRC / "pretask").glob("*.py"))
RUNTIME_FILES = sorted(p.name for p in (SRC / "runtime").glob("*.py"))


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("groundtruth/__init__.py", "")
        for name in PRETASK_FILES:
            zf.writestr(
                f"groundtruth/pretask/{name}",
                (SRC / "pretask" / name).read_text(encoding="utf-8"),
            )
        for name in RUNTIME_FILES:
            zf.writestr(
                f"groundtruth/runtime/{name}",
                (SRC / "runtime" / name).read_text(encoding="utf-8"),
            )
    return buf.getvalue()


def main() -> None:
    payload = base64.b64encode(build_zip()).decode("ascii")
    wrapped = "\n".join(textwrap.wrap(payload, 76))
    out = TEMPLATE.replace("__PAYLOAD__", wrapped)
    OUT.write_text(out, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, payload {len(payload)} chars)")


TEMPLATE = '''#!/usr/bin/env python3
"""Self-contained GT pre-task brief V1R bundle.

V1R: map-only, inject-once, stay-silent.
Output: ranked files + functions + test mappings. No prose, no constraints.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import zipfile
from pathlib import Path

_PAYLOAD = """
__PAYLOAD__
"""


def _bootstrap() -> None:
    target = Path('/tmp/gt_pretask_v1r_pkg')
    sentinel = target / 'groundtruth' / 'pretask' / 'v1r_brief.py'
    if not sentinel.exists():
        target.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(''.join(_PAYLOAD.split()))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(target)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def main(argv: list[str] | None = None) -> int:
    _bootstrap()

    parser = argparse.ArgumentParser(description='GT pre-task brief V1R bundle')
    parser.add_argument('--db', required=True)
    parser.add_argument('--root', default='/workspace')
    parser.add_argument('--issue-text-file', required=True)
    parser.add_argument('--telemetry-out', default='')
    parser.add_argument('--max-files', type=int, default=5)
    parser.add_argument('--max-funcs-per-file', type=int, default=3)
    parser.add_argument('--task-id', default='unknown')
    parser.add_argument('--gold-paths-out', default='',
                        help='If set, write one ranked candidate path per line to this file. '
                             'Used by TEG-1 commit-prompt hook.')
    args, _unknown = parser.parse_known_args(argv)

    db_arg = args.db
    if not os.path.exists(db_arg):
        for fallback in ['/tmp/gt_graph.db', '/tmp/gt_index.db']:
            if os.path.exists(fallback):
                db_arg = fallback
                break

    if not os.path.exists(db_arg):
        return 0

    issue_text = ''
    if os.path.exists(args.issue_text_file):
        issue_text = Path(args.issue_text_file).read_text(encoding='utf-8', errors='replace')
    if not issue_text.strip():
        return 0

    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        result = generate_v1r_brief(
            issue_text=issue_text,
            repo_root=args.root,
            graph_db=db_arg,
            bug_id=args.task_id,
            max_files=args.max_files,
            max_brief_tokens=400,
        )
        if result.brief_text.strip():
            print(result.brief_text)

        if args.gold_paths_out and result.files:
            try:
                with open(args.gold_paths_out, 'w', encoding='utf-8') as f:
                    for fobj in result.files:
                        if fobj.path:
                            f.write(fobj.path + '\\n')
            except OSError:
                pass

        if args.telemetry_out and result.v74_result:
            from dataclasses import asdict
            telemetry = {
                'brief_mode': 'v1r',
                'files': [{'path': f.path, 'score': f.score, 'functions': f.functions} for f in result.files],
                'token_estimate': result.token_estimate,
                'v74_focus_set': result.v74_result.focus_set,
                'v74_gold_in_focus': result.v74_result.gold_in_focus,
                'v74_first_gold_rank': result.v74_result.first_gold_rank_full,
            }
            with open(args.telemetry_out, 'a') as f:
                f.write(json.dumps(telemetry) + '\\n')
    except Exception as e:
        print(f'V1R brief error: {e}', file=sys.stderr)
        return 0

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
'''


if __name__ == "__main__":
    main()
