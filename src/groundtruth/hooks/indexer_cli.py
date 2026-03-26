"""Pre-index CLI — builds SQLite symbol index + reference graph before agent starts.

Indexes both SYMBOLS (functions, classes, methods) and REFS (import relationships)
so that find_callers(), get_importers_of_file(), and test discovery all work.

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
        from groundtruth.index.ast_parser import parse_python_file, parse_python_imports
        from groundtruth.utils.result import Ok

        store = SymbolStore(args.db)
        store.initialize()

        skip_dirs = {".git", "__pycache__", "node_modules", ".tox", ".eggs",
                     "venv", "env", "build", "dist", ".mypy_cache", ".pytest_cache"}
        files_indexed = 0
        symbols_indexed = 0
        refs_indexed = 0
        max_time = 60  # seconds budget (increased for large repos)

        # Collect all Python files, sort: source first, then tests
        # (both need indexing, but source symbols must exist before refs)
        all_files: list[tuple[str, str]] = []  # (fpath, relpath)
        for dirpath, dirnames, filenames in os.walk(args.root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                relpath = os.path.relpath(fpath, args.root).replace("\\", "/")
                try:
                    if os.path.getsize(fpath) > 500_000:
                        continue
                except OSError:
                    continue
                all_files.append((fpath, relpath))

        # Sort: non-test files first (need their symbols for ref resolution),
        # then test files (need their imports for test discovery)
        def _is_test(rp: str) -> bool:
            return "/test" in rp.lower() or rp.lower().startswith("test")
        all_files.sort(key=lambda x: (1 if _is_test(x[1]) else 0, x[1]))

        now = int(time.time())
        for fpath, relpath in all_files:
            if time.time() - start > max_time * 0.7:  # use 70% of budget for symbols
                break
            try:
                symbols = parse_python_file(fpath)
            except Exception:
                continue

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

        # Phase 2: Index import refs (links importing files to defined symbols)
        for fpath, relpath in all_files:
            if time.time() - start > max_time:
                break
            try:
                imports = parse_python_imports(fpath)
            except Exception:
                continue

            for imp in imports:
                if not imp.name or len(imp.name) <= 1:
                    continue
                try:
                    # Find the symbol that's being imported
                    result = store.find_symbol_by_name(imp.name)
                    if isinstance(result, Ok) and result.value:
                        symbol_id = result.value[0].id
                        store.insert_ref(
                            symbol_id=symbol_id,
                            referenced_in_file=relpath,
                            referenced_at_line=imp.line,
                            reference_type="import",
                        )
                        refs_indexed += 1
                except Exception:
                    continue

        elapsed = round(time.time() - start, 2)
        print(f"INDEX_READY {elapsed}s {files_indexed} files {symbols_indexed} symbols {refs_indexed} refs")

    except Exception as e:
        elapsed = round(time.time() - start, 2)
        print(f"INDEX_FAILED {elapsed}s: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
