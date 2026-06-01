#!/usr/bin/env python3
"""Manual LSP test: start server, initialize, open file, query definition."""
import asyncio
import os
import sys
import time

sys.path.insert(0, "/workspaces/groundtruth/src")

TESTS = {
    "go": {
        "root": "/tmp/gt_5lang/crossplane",
        "ext": ".go",
        "file": "internal/engine/engine.go",
        "symbol": "Reconcile",
        "lang_id": "go",
    },
    "rust": {
        "root": "/tmp/gt_5lang/axum",
        "ext": ".rs",
        "file": "axum/src/routing/mod.rs",
        "symbol": "route",
        "lang_id": "rust",
    },
    "python": {
        "root": "/tmp/gt_5lang/beets",
        "ext": ".py",
        "file": "beets/dbcore/db.py",
        "symbol": "query",
        "lang_id": "python",
    },
    "typescript": {
        "root": "/tmp/gt_5lang/hono",
        "ext": ".ts",
        "file": "src/request.ts",
        "symbol": "json",
        "lang_id": "typescript",
    },
}


async def test_one(lang: str) -> None:
    from groundtruth.lsp.client import LSPClient
    from groundtruth.lsp.config import get_server_config
    from groundtruth.utils.result import Err

    info = TESTS[lang]
    root = info["root"]
    root_uri = f"file://{root}"
    cfg = get_server_config(info["ext"])
    if isinstance(cfg, Err):
        print(f"  [{lang}] No server config for {info['ext']}")
        return

    print(f"  [{lang}] Server: {cfg.value.command}")
    client = LSPClient(cfg.value.command, root_uri)

    # Start
    r = await client.start()
    if isinstance(r, Err):
        print(f"  [{lang}] Start FAILED: {r.error.message}")
        return
    print(f"  [{lang}] Process started")

    # Initialize
    init = await client.send_request("initialize", {
        "processId": os.getpid(),
        "rootUri": root_uri,
        "capabilities": {
            "textDocument": {
                "definition": {},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
            },
            "workspace": {"workspaceFolders": True},
        },
        "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(root)}],
    }, timeout=60.0)

    if isinstance(init, Err):
        print(f"  [{lang}] Initialize FAILED: {init.error.message}")
        # Check stderr
        if client._process and client._process.stderr:
            try:
                stderr = await asyncio.wait_for(client._process.stderr.read(2000), timeout=2.0)
                print(f"  [{lang}] Server stderr: {stderr.decode('utf-8', errors='replace')[:500]}")
            except Exception:
                pass
        return

    print(f"  [{lang}] Initialize OK")
    await client.send_notification("initialized", {})
    await client.drain(timeout=2.0)

    # Wait for server to be ready
    print(f"  [{lang}] Waiting for server to load project...")
    t0 = time.time()
    await client.wait_for_progress_complete(timeout=60.0)
    elapsed = time.time() - t0
    print(f"  [{lang}] Server ready ({elapsed:.1f}s)")

    # Open file
    filepath = os.path.join(root, info["file"])
    if not os.path.exists(filepath):
        print(f"  [{lang}] File not found: {filepath}")
        await client.shutdown()
        return

    with open(filepath, encoding="utf-8", errors="replace") as f:
        text = f.read()

    uri = f"file://{filepath}"
    await client.did_open(uri, info["lang_id"], 1, text)
    print(f"  [{lang}] Opened {info['file']}")

    # Small delay for server to process
    await asyncio.sleep(2.0)
    await client.drain(timeout=2.0)

    # Find symbol and query definition
    symbol = info["symbol"]
    lines = text.split("\n")
    found = False
    for i, line in enumerate(lines):
        if symbol in line and not line.strip().startswith("//") and not line.strip().startswith("#"):
            col = line.find(symbol)
            print(f"  [{lang}] Querying definition for '{symbol}' at {info['file']}:{i+1}:{col}")
            defn = await client.definition(uri, i, col, timeout=30.0)
            if isinstance(defn, Err):
                print(f"  [{lang}] Definition FAILED: {defn.error.message}")
            elif not defn.value:
                print(f"  [{lang}] Definition: EMPTY (no locations returned)")
            else:
                for loc in defn.value[:3]:
                    target = loc.uri.replace("file://", "")
                    try:
                        target = os.path.relpath(target, root)
                    except ValueError:
                        pass
                    print(f"  [{lang}] Definition -> {target}:{loc.range.start.line+1}")
            found = True
            break

    if not found:
        print(f"  [{lang}] Symbol '{symbol}' not found in file")

    # Also test hover
    for i, line in enumerate(lines):
        if symbol in line and not line.strip().startswith("//") and not line.strip().startswith("#"):
            col = line.find(symbol)
            hover = await client.hover(uri, i, col, timeout=10.0)
            if isinstance(hover, Err):
                print(f"  [{lang}] Hover FAILED: {hover.error.message}")
            elif hover.value is None:
                print(f"  [{lang}] Hover: NULL")
            else:
                h = hover.value
                if hasattr(h.contents, 'value'):
                    print(f"  [{lang}] Hover: {h.contents.value[:150]}")
                else:
                    print(f"  [{lang}] Hover: {str(h.contents)[:150]}")
            break

    await client.shutdown()
    print(f"  [{lang}] DONE")


async def main():
    lang = sys.argv[1] if len(sys.argv) > 1 else None
    langs = [lang] if lang else list(TESTS.keys())

    for l in langs:
        print(f"\n{'='*50}")
        print(f"=== Testing {l.upper()} LSP ===")
        print(f"{'='*50}")
        try:
            await test_one(l)
        except Exception as e:
            print(f"  [{l}] EXCEPTION: {type(e).__name__}: {e}")


asyncio.run(main())
