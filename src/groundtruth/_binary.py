"""Auto-download and cache the gt-index binary from GitHub Releases.

gt-index is a Go binary that indexes source code for all languages using
tree-sitter. It's the multi-language indexer that powers GroundTruth.

On first use, this module downloads the correct platform binary from GitHub
and caches it at ~/.groundtruth/bin/. Subsequent runs use the cached binary.
"""

from __future__ import annotations

import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

GITHUB_REPO = "harneet2512/groundtruth"
CACHE_DIR = Path.home() / ".groundtruth" / "bin"

# Map (system, machine) to GitHub Release asset name
_PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Linux", "x86_64"): "gt-index-linux-amd64.tar.gz",
    ("Linux", "aarch64"): "gt-index-linux-arm64.tar.gz",
    ("Darwin", "arm64"): "gt-index-darwin-arm64.tar.gz",
    ("Darwin", "x86_64"): "gt-index-darwin-amd64.tar.gz",
    ("Windows", "AMD64"): "gt-index-windows-amd64.zip",
}

# Version of gt-index to download (updated on each release)
GT_INDEX_VERSION = "v1.1.0"


def _binary_name() -> str:
    return "gt-index.exe" if sys.platform == "win32" else "gt-index"


def _get_asset_name() -> str:
    system = platform.system()
    machine = platform.machine()
    key = (system, machine)
    if key not in _PLATFORM_MAP:
        raise RuntimeError(
            f"Unsupported platform: {system}/{machine}. "
            f"Supported: {', '.join(f'{s}/{m}' for s, m in _PLATFORM_MAP)}"
        )
    return _PLATFORM_MAP[key]


def _download_url(version: str, asset: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{version}/{asset}"


def _extract(archive_path: Path, dest_dir: Path) -> Path:
    """Extract archive and return path to the binary."""
    bin_name = _binary_name()
    if str(archive_path).endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest_dir)

    # Find the binary (may be at root or one level deep)
    binary = dest_dir / bin_name
    if not binary.exists():
        for child in dest_dir.iterdir():
            if child.is_dir():
                candidate = child / bin_name
                if candidate.exists():
                    shutil.move(str(candidate), str(binary))
                    break
            elif child.name == bin_name:
                binary = child
                break

    if sys.platform != "win32" and binary.exists():
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    return binary


def ensure_binary(version: str | None = None) -> str:
    """Return path to gt-index binary, downloading if needed."""
    version = version or GT_INDEX_VERSION
    versioned_dir = CACHE_DIR / version
    binary = versioned_dir / _binary_name()

    if binary.exists():
        return str(binary)

    # Download
    asset = _get_asset_name()
    url = _download_url(version, asset)

    versioned_dir.mkdir(parents=True, exist_ok=True)
    archive_path = versioned_dir / asset

    sys.stderr.write(
        f"GroundTruth: downloading gt-index {version} "
        f"for {platform.system()}/{platform.machine()}...\n"
    )
    try:
        urllib.request.urlretrieve(url, archive_path)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download gt-index from {url}: {exc}\n"
            f"You can build it manually: cd gt-index && CGO_ENABLED=1 go build -o gt-index ./cmd/gt-index/"
        ) from exc

    binary = _extract(archive_path, versioned_dir)
    archive_path.unlink(missing_ok=True)

    if not binary.exists():
        raise RuntimeError(
            f"gt-index binary not found after extraction. "
            f"Expected at {binary}"
        )

    sys.stderr.write(f"GroundTruth: gt-index installed at {binary}\n")
    return str(binary)


def find_binary() -> str:
    """Find gt-index: check PATH first, then local build, then cache/download.

    Search order:
    1. On PATH (user installed gt-index globally)
    2. ./gt-index/gt-index[.exe] (local build in repo)
    3. ~/.groundtruth/bin/{version}/gt-index (cached download)
    """
    # 1. Check PATH
    on_path = shutil.which("gt-index")
    if on_path:
        return on_path

    # 2. Check local build (common during development)
    local = Path("gt-index") / _binary_name()
    if local.exists():
        return str(local.resolve())

    # 3. Download/cache
    return ensure_binary()


def run_index(root: str, output: str, timeout: int = 600) -> bool:
    """Run gt-index on a directory. Returns True on success."""
    try:
        binary = find_binary()
    except RuntimeError as exc:
        sys.stderr.write(f"GroundTruth: {exc}\n")
        return False

    try:
        result = subprocess.run(
            [binary, "-root", root, "-output", output],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            sys.stderr.write(f"GroundTruth: gt-index failed: {result.stderr[:500]}\n")
            return False
        return True
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"GroundTruth: gt-index timed out after {timeout}s\n")
        return False
    except FileNotFoundError:
        sys.stderr.write(f"GroundTruth: gt-index binary not found at {binary}\n")
        return False
