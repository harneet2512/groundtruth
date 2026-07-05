"""Fable LSP8: the pyright shim config GT drops for go-to-definition must NOT persist in the
agent's working repo. `_write_pyright_shim_config` returns a cleanup callable that removes exactly
the file GT created (registered via atexit in `_resolve_edges`), and only writes one when the repo
has none of its own.

Mutation check: replacing `os.remove(_p)` in the returned `_cleanup` with `pass` (or dropping the
atexit registration) leaves the shim in the repo → the cleanup assertion reddens.
"""
import os
from types import SimpleNamespace

from groundtruth.resolve import _write_pyright_shim_config


def _cfg(cmd="pyright-langserver"):
    return SimpleNamespace(command=[cmd, "--stdio"])


def test_lsp8_shim_written_then_cleaned(tmp_path):
    root = str(tmp_path)
    cleanup = _write_pyright_shim_config(root, _cfg())
    cfg_path = os.path.join(root, "pyrightconfig.json")
    assert cleanup is not None, "pyright + no config → GT must write a shim AND return a cleanup"
    assert os.path.exists(cfg_path), "shim must exist during the LSP session"
    cleanup()
    assert not os.path.exists(cfg_path), (
        "LSP8: GT's pyright shim must be cleaned, never left in the agent's working repo"
    )


def test_lsp8_no_write_when_repo_owns_config(tmp_path):
    root = str(tmp_path)
    cfg_path = os.path.join(root, "pyrightconfig.json")
    with open(cfg_path, "w") as f:
        f.write('{"pythonVersion":"3.9"}')
    assert _write_pyright_shim_config(root, _cfg()) is None, "must not shadow the repo's own config"
    assert "3.9" in open(cfg_path).read(), "the repo's own config must be untouched"


def test_lsp8_no_write_when_pyproject_has_tool_pyright(tmp_path):
    root = str(tmp_path)
    with open(os.path.join(root, "pyproject.toml"), "w") as f:
        f.write("[tool.pyright]\npythonVersion='3.9'\n")
    assert _write_pyright_shim_config(root, _cfg()) is None
    assert not os.path.exists(os.path.join(root, "pyrightconfig.json"))


def test_lsp8_no_write_for_non_pyright_server(tmp_path):
    root = str(tmp_path)
    assert _write_pyright_shim_config(root, _cfg("gopls")) is None
    assert not os.path.exists(os.path.join(root, "pyrightconfig.json"))
