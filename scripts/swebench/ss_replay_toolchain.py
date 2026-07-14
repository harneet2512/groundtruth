"""Digest-pinned toolchain executors for SS replay.

Replay must not parse a recorded edit with the host interpreter: Python's native
traceback bytes vary by interpreter version. This module loads explicit,
digest-pinned reproducer metadata and exposes only the parse-only command contract
used by ``groundtruth.runtime.edit_check``. The arm-4 artifacts did not record task
image digests, so these are behaviorally exact immutable reproducers established by
the workflow mapping plus recorded runtime bytes, not claimed historical digests.
Missing or mismatched metadata is quiet (``rc=None``); no executor falls back to
the host.  Verification execution is enabled only by the separately installed,
strict ``pytest <one file>`` plus differential-worktree boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable


Executor = Callable[[list[str], str, int], tuple[int | None, str, str]]
MutationExecutor = Callable[[str, str, int], tuple[int | None, str, str]]

_SCHEMA = "gt.ss_replay_toolchains.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:([0-9a-f]{64})$")
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PY_PARSE_CODE = "import ast,sys; ast.parse(open(sys.argv[1],'rb').read(), sys.argv[1])"
_SED_INPLACE_RE = re.compile(r"^\s*sed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place(?:=[^\s]+)?)(?:\s|$)")
_BASE_WORKTREE_ROOT = "/gt-replay-bases"
_BASE_WORKTREE_NAME_RE = re.compile(r"^\.gt_base_[0-9a-f]{12}$")


def load_replay_toolchain(task: str, metadata_path: Path) -> dict[str, str] | None:
    """Return one validated immutable runtime record, or ``None`` fail-closed."""
    try:
        payload = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        if payload.get("schema") != _SCHEMA or not isinstance(payload.get("tasks"), dict):
            return None
        raw = payload["tasks"].get(task)
        if not isinstance(raw, dict):
            return None
        record: dict[str, str] = {}
        for key in (
            "image", "image_config_sha256", "platform", "repo_root",
            "python_version", "python_env_sha256",
        ):
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                return None
            record[key] = value
        image_match = _IMAGE_RE.fullmatch(record["image"])
        if image_match is None:
            return None
        if not _SHA256_RE.fullmatch(record["image_config_sha256"]):
            return None
        if not _SHA256_RE.fullmatch(record["python_env_sha256"]):
            return None
        if record["platform"] != "linux/amd64":
            return None
        root = PurePosixPath(record["repo_root"])
        if (not root.is_absolute() or len(root.parts) <= 1 or ".." in root.parts
                or "," in str(root) or str(root) != record["repo_root"]):
            return None
        if _PYTHON_VERSION_RE.fullmatch(record["python_version"]) is None:
            return None
        return record
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _quiet_executor(reason: str) -> Executor:
    def _quiet(_cmd: list[str], _cwd: str, _timeout: int) -> tuple[None, str, str]:
        return None, "", f"replay_toolchain_unavailable:{reason}"

    return _quiet


def _inspect_matches(record: dict[str, str]) -> bool:
    """Verify the locally cached immutable image and its recorded runtime config.

    Classic Docker reports the config digest as ``Id``. Docker Desktop's
    containerd store reports the manifest digest there instead. The latter is
    accepted only when ``RepoDigests`` independently contains the exact immutable
    image reference. No mutable tag and no implicit pull is accepted.
    """
    image = record["image"]
    manifest = "sha256:" + image.rsplit("@sha256:", 1)[1]
    config = "sha256:" + record["image_config_sha256"]
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{json .}}"],
            capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=30,
        )
        if result.returncode != 0:
            return False
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            return False
        repo_digests = info.get("RepoDigests") or []
        if image not in repo_digests:
            return False
        image_id = str(info.get("Id") or "")
        if image_id not in (config, manifest):
            return False
        if str(info.get("Os") or "") != "linux":
            return False
        if str(info.get("Architecture") or "") != "amd64":
            return False
        cfg = info.get("Config")
        if not isinstance(cfg, dict) or cfg.get("WorkingDir") != record["repo_root"]:
            return False
        env = cfg.get("Env") or []
        if f"PYTHON_VERSION={record['python_version']}" not in env:
            return False
        if f"PYTHON_SHA256={record['python_env_sha256']}" not in env:
            return False
        return True
    except (OSError, UnicodeError, subprocess.SubprocessError, TypeError, ValueError,
            json.JSONDecodeError):
        return False


def _confined_target(file_arg: str, cwd: str, container_root: str) -> tuple[Path, str] | None:
    """Map one existing host source file to its canonical recorded container path."""
    if not file_arg or not cwd or "," in file_arg or "," in cwd:
        return None
    try:
        root = Path(cwd).resolve(strict=True)
        raw = Path(file_arg)
        source = (raw if raw.is_absolute() else root / raw).resolve(strict=True)
        relative = source.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if (not source.is_file() or "," in str(root) or "," in str(source)
            or any(part == ".." for part in relative.parts)):
        return None
    target = str(PurePosixPath(container_root).joinpath(*relative.parts))
    if "," in target:
        return None
    return source, target


def build_replay_edit_executor(record: dict[str, str] | None) -> Executor:
    """Build a parser-only executor over an already-cached immutable task image.

    The returned function accepts exactly edit_check's Python ``ast.parse`` argv.
    Every other command returns unavailable without spawning a container.
    """
    if record is None or not _inspect_matches(record):
        return _quiet_executor("image_or_config_mismatch")

    image = record["image"]
    container_root = record["repo_root"]

    def _executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int | None, str, str]:
        expected_prefix = ["python", "-I", "-c", _PY_PARSE_CODE]
        if type(cmd) is not list or len(cmd) != 5 or cmd[:4] != expected_prefix:
            return None, "", "replay_toolchain_unsupported_command"
        mapped = _confined_target(cmd[4], cwd, container_root)
        if mapped is None:
            return None, "", "replay_toolchain_unsupported_target"
        source, target = mapped
        mount = f"type=bind,source={source},target={target},readonly"
        argv = [
            "docker", "run", "--rm", "--pull", "never",
            "--network", "none", "--read-only",
            "--platform", record["platform"], "--workdir", container_root,
            "--mount", mount, "--entrypoint", "", image,
            "python", "-I", "-c", _PY_PARSE_CODE, target,
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="strict",
                timeout=max(1, int(timeout)) + 5,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return None, "", "replay_toolchain_timeout"
        except (OSError, UnicodeError, TypeError, ValueError):
            return None, "", "replay_toolchain_spawn_error"
        if type(result.returncode) is not int:
            return None, "", "replay_toolchain_invalid_result"
        if type(result.stdout) is not str or type(result.stderr) is not str:
            return None, "", "replay_toolchain_invalid_result"
        return result.returncode, result.stdout, result.stderr

    return _executor


def build_replay_mutation_executor(record: dict[str, str] | None) -> MutationExecutor:
    """Build a replay-only GNU-sed executor in the validated immutable task image.

    Only preflighted ``sed -i`` mutations are admitted.  The caller owns shell/target
    confinement before this boundary; this executor independently confines the writable
    bind to the replay checkout and never falls back to host shell semantics.
    """
    if record is None or not _inspect_matches(record):
        quiet = _quiet_executor("image_or_config_mismatch")
        return lambda _command, cwd, timeout: quiet([], cwd, timeout)

    image = record["image"]
    container_root = record["repo_root"]

    def _executor(command: str, cwd: str, timeout: int) -> tuple[int | None, str, str]:
        if type(command) is not str or not _SED_INPLACE_RE.match(command):
            return None, "", "replay_toolchain_unsupported_command"
        if not cwd or "," in cwd:
            return None, "", "replay_toolchain_unsupported_target"
        try:
            workspace = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None, "", "replay_toolchain_unsupported_target"
        if not workspace.is_dir() or "," in str(workspace):
            return None, "", "replay_toolchain_unsupported_target"
        mount = f"type=bind,source={workspace},target={container_root}"
        argv = [
            "docker", "run", "--rm", "--pull", "never",
            "--network", "none", "--read-only",
            "--platform", record["platform"], "--workdir", container_root,
            "--mount", mount, "--tmpfs", "/tmp:rw,nosuid,nodev",
            "--entrypoint", "/bin/bash", image, "-c", command,
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="strict",
                timeout=max(1, int(timeout)) + 5,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            return None, "", "replay_toolchain_timeout"
        except (OSError, UnicodeError, TypeError, ValueError):
            return None, "", "replay_toolchain_spawn_error"
        if (type(result.returncode) is not int or type(result.stdout) is not str
                or type(result.stderr) is not str):
            return None, "", "replay_toolchain_invalid_result"
        return result.returncode, result.stdout, result.stderr

    return _executor


def build_replay_verification_executor(
        record: dict[str, str] | None, workspace: Path) -> Executor | None:
    """Build the bounded covering-test executor for replay.

    The model-facing verification surface admits only the canonical Python
    covering command ``pytest <one repo test file>``.  The executor also admits the
    exact internal git-worktree lifecycle used by differential attribution.  Both
    the edited checkout and a dedicated worktree parent are bind-mounted into the
    immutable image; arbitrary git/rm/pytest argv remain unavailable.
    """
    if record is None or not _inspect_matches(record):
        return None
    try:
        root = Path(workspace).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not root.is_dir() or "," in str(root):
        return None
    base_host_root = root.parent / ".gt_replay_bases"
    try:
        base_host_root.mkdir(exist_ok=True)
        base_host_root = base_host_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not base_host_root.is_dir() or "," in str(base_host_root):
        return None
    image = record["image"]
    container_root = record["repo_root"]

    def _base_name(path: str) -> str | None:
        pure = PurePosixPath(path)
        if (str(pure.parent) != _BASE_WORKTREE_ROOT
                or _BASE_WORKTREE_NAME_RE.fullmatch(pure.name) is None
                or str(pure) != path):
            return None
        return pure.name

    def _host_cwd(cwd: str) -> Path | None:
        if cwd == container_root:
            return root
        name = _base_name(cwd)
        if name is None:
            return None
        try:
            candidate = (base_host_root / name).resolve(strict=True)
            candidate.relative_to(base_host_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate if candidate.is_dir() else None

    def _pytest_target(cmd: list[str], cwd: str) -> bool:
        if (type(cmd) is not list or len(cmd) != 2 or cmd[0] != "pytest"
                or type(cmd[1]) is not str):
            return False
        rel = PurePosixPath(cmd[1])
        if (rel.is_absolute() or str(rel) != cmd[1] or ".." in rel.parts
                or "::" in cmd[1] or "," in cmd[1]):
            return False
        host_cwd = _host_cwd(cwd)
        if host_cwd is None:
            return False
        try:
            target = host_cwd.joinpath(*rel.parts).resolve(strict=True)
            target.relative_to(host_cwd)
        except (OSError, RuntimeError, ValueError):
            return False
        return target.is_file()

    def _internal_command(cmd: list[str], cwd: str) -> bool:
        if cwd != container_root or type(cmd) is not list:
            return False
        prefix = ["git", "-C", container_root]
        if cmd == [*prefix, "rev-parse", "--verify", "HEAD"]:
            return True
        if cmd == [*prefix, "worktree", "prune"]:
            return True
        if len(cmd) == 8 and cmd[:6] == [*prefix, "worktree", "add", "--detach"]:
            return _base_name(cmd[6]) is not None and cmd[7] == "HEAD"
        if len(cmd) == 7 and cmd[:6] == [*prefix, "worktree", "remove", "--force"]:
            return _base_name(cmd[6]) is not None
        if len(cmd) == 3 and cmd[:2] == ["rm", "-rf"]:
            return _base_name(cmd[2]) is not None
        return False

    def _executor(cmd: list[str], cwd: str, timeout: int) -> tuple[int | None, str, str]:
        is_pytest = _pytest_target(cmd, cwd)
        is_internal = _internal_command(cmd, cwd)
        if not is_pytest and not is_internal:
            if type(cmd) is list and cmd and cmd[0] == "pytest" and _host_cwd(cwd) is None:
                return None, "", "replay_verification_unsupported_cwd"
            if type(cmd) is list and cmd and cmd[0] == "pytest":
                return None, "", "replay_verification_unsupported_command"
            return None, "", "replay_verification_unsupported_command"
        run_cwd = cwd if is_pytest else container_root
        if _host_cwd(run_cwd) is None and run_cwd != container_root:
            return None, "", "replay_verification_unsupported_cwd"
        repo_mount = f"type=bind,source={root},target={container_root}"
        base_mount = (
            f"type=bind,source={base_host_root},target={_BASE_WORKTREE_ROOT}")
        argv = [
            "docker", "run", "--rm", "--pull", "never",
            "--network", "none", "--read-only",
            "--platform", record["platform"], "--workdir", run_cwd,
            "--mount", repo_mount, "--mount", base_mount,
            "--tmpfs", "/tmp:rw,nosuid,nodev",
            "--entrypoint", "", image, *cmd,
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="strict",
                timeout=max(1, int(timeout)) + 5,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired:
            raise
        except (OSError, UnicodeError, TypeError, ValueError):
            return None, "", "replay_verification_spawn_error"
        if (type(result.returncode) is not int or type(result.stdout) is not str
                or type(result.stderr) is not str):
            return None, "", "replay_verification_invalid_result"
        return result.returncode, result.stdout, result.stderr

    setattr(_executor, "_gt_base_worktree_root", _BASE_WORKTREE_ROOT)
    return _executor


def install_replay_edit_executor(seam: Any, task: str, metadata_path: Path) -> bool:
    """Install the replay-only executor at the seam's existing executor boundary.

    ``GT_VERIFY_EXECUTE`` remains independently off in the replay child.  A task
    without explicit runtime metadata receives the quiet executor, never the host
    fallback.  Returns whether validated metadata existed (not whether an image was
    available); image/config mismatch still produces a quiet executor.
    """
    if not callable(getattr(seam, "_build_edit_check_executor", None)):
        raise RuntimeError("replay seam lacks dedicated edit-check executor boundary")
    record = load_replay_toolchain(task, metadata_path)
    executor = build_replay_edit_executor(record)
    seam._build_edit_check_executor = lambda: executor
    return record is not None


def install_replay_verification_executor(
        seam: Any, task: str, metadata_path: Path, workspace: Path) -> bool:
    """Install verification execution only when immutable runtime readiness is proven."""
    if not callable(getattr(seam, "_build_verification_executor", None)):
        os.environ["GT_VERIFY_EXECUTE"] = "0"
        return False
    record = load_replay_toolchain(task, metadata_path)
    executor = build_replay_verification_executor(record, workspace)
    ready = executor is not None
    os.environ["GT_VERIFY_EXECUTE"] = "1" if ready else "0"
    seam._build_verification_executor = lambda: executor
    return ready
