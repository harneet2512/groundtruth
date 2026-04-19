"""Auto-download and cache the gt-index binary from GitHub Releases.

gt-index is a Go binary that indexes source code for all languages using
tree-sitter. It's the multi-language indexer that powers GroundTruth.

On first use, this module downloads the correct platform binary from GitHub
and caches it at ~/.groundtruth/bin/. Subsequent runs use the cached binary.
"""

from __future__ import annotations

import hashlib
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


def _fetch_sha256sums(version: str) -> dict[str, str]:
    """Download SHA256SUMS from the release and return {filename: hex_digest} mapping.

    Returns an empty dict if the file is unreachable (e.g. older releases without it).
    """
    url = f"https://github.com/{GITHUB_REPO}/releases/download/{version}/SHA256SUMS"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8")
        result: dict[str, str] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2:
                digest, filename = parts
                result[filename.lstrip("*")] = digest  # some tools prefix '*'
        return result
    except Exception:
        return {}


def _verify_checksum(archive_path: Path, asset_name: str, expected_hex: str) -> None:
    """Verify archive SHA256. Removes the bad file and raises RuntimeError on mismatch."""
    actual_hex = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_hex.lower() != expected_hex.lower():
        archive_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Checksum mismatch for {asset_name}.\n"
            f"  Expected: {expected_hex}\n"
            f"  Got:      {actual_hex}\n"
            "The downloaded archive may be corrupt or tampered with. "
            "Delete ~/.groundtruth/bin and retry."
        )


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

    # Verify integrity before extracting. Fetch SHA256SUMS from the same release.
    checksums = _fetch_sha256sums(version)
    if checksums:
        expected = checksums.get(asset)
        if expected is None:
            sys.stderr.write(
                f"GroundTruth: WARNING — {asset} not listed in SHA256SUMS; "
                "skipping integrity check.\n"
            )
        else:
            _verify_checksum(archive_path, asset, expected)
            sys.stderr.write(f"GroundTruth: checksum verified for {asset}\n")
    else:
        sys.stderr.write(
            "GroundTruth: SHA256SUMS not available for this release; "
            "skipping integrity check.\n"
        )

    binary = _extract(archive_path, versioned_dir)
    archive_path.unlink(missing_ok=True)

    if not binary.exists():
        raise RuntimeError(f"gt-index binary not found after extraction. Expected at {binary}")

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
            capture_output=True,
            text=True,
            timeout=timeout,
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
