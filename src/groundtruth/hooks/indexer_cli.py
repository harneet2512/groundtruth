"""Pre-index CLI — builds SQLite symbol index before agent starts.

Usage:
    python -m groundtruth.hooks.indexer_cli --root=/testbed --db=/tmp/gt_index.db
"""

from __future__ import annotations

import argparse
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="GT index builder")
    parser.add_argument("--root", default="/testbed", help="Repository root")
    parser.add_argument("--db", default="/tmp/gt_index.db", help="Output database path")
    args = parser.parse_args()

    start = time.time()

    try:
        from groundtruth.index.store import SymbolStore
        from groundtruth.index.ast_parser import parse_python_file

        store = SymbolStore(args.db)
        result = store.initialize()

        # Walk Python files and index them
        skip_dirs = {".git", "__pycache__", "node_modules", ".tox", ".eggs",
                     "venv", "env", "build", "dist", ".mypy_cache", ".pytest_cache"}
        files_indexed = 0
        symbols_indexed = 0
        max_time = 30  # seconds budget

        for dirpath, dirnames, filenames in os.walk(args.root):
            # Prune skip dirs
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]

            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                if time.time() - start > max_time:
                    break

                fpath = os.path.join(dirpath, fname)
                relpath = os.path.relpath(fpath, args.root)

                try:
                    if os.path.getsize(fpath) > 750_000:
                        continue
                except OSError:
                    continue

                try:
                    symbols = parse_python_file(fpath)
                except Exception:
                    continue

                now = int(time.time())
                for sym in symbols:
                    try:
                        store.insert_symbol(
                            name=sym.name,
                            kind=sym.kind,
                            language="python",
                            file_path=relpath,
                            line_number=sym.line,
                            end_line=sym.end_line,
                            is_exported=sym.is_exported,
                            signature=sym.signature,
                            params=None,
                            return_type=sym.return_type,
                            documentation=sym.documentation,
                            last_indexed_at=now,
                        )
                        symbols_indexed += 1
                    except Exception:
                        continue

                files_indexed += 1

            if time.time() - start > max_time:
                break

        elapsed = round(time.time() - start, 2)
        print(f"INDEX_READY {elapsed}s {files_indexed} files {symbols_indexed} symbols")

    except Exception as e:
        elapsed = round(time.time() - start, 2)
        print(f"INDEX_FAILED {elapsed}s: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
