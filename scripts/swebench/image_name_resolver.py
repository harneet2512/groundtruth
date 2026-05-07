"""Deterministic SWE-Bench-Live image-name resolver.

Why
---
Per-task evaluation needs the right Docker image tag. Hardcoding tag prefixes
(``starryzhang/sweb.eval.x86_64.{owner}_1776_{repo}-{pull_number}:latest``)
binds the runner to a single SWE-Bench-Live release tag (e.g. ``1776``) and
breaks the moment SWE-Bench-Live ships a new tag. This resolver:

  1. Trusts the dataset row first (if it carries ``image_name``, use it).
     RC-17 (F-002): if that ``image_name`` carries a literal ``:latest``
     tag we refuse it loudly — ``:latest`` is a floating tag and silently
     drifts as SWE-Bench-Live re-publishes images. The operator must
     materialize a digest first (see ``capture_image_digest`` below and
     the ``--image-digests-file`` runner flag).
  2. Otherwise queries local Docker via a caller-injected glob function for
     candidate images matching the repo + pull-number pattern. The tag slot
     is a wildcard — works for ``1776``, ``1900``, or whatever ships next.
  3. Picks the lexicographically last match (stable, latest-first ordering
     when SWE-Bench-Live tags are zero-padded ascending integers).
  4. Returns ``None`` if nothing matches — caller surfaces as a per-task
     blocker, not a silent failure.

Digest pinning (RC-17 / F-002)
------------------------------
``capture_image_digest(image)`` shells out to ``docker inspect --format
'{{.Id}}'`` and returns ``"<image>@sha256:<digest>"`` so future runs resolve
to the exact same content-addressable image regardless of registry tag
movement. ``apply_digest_overrides(instance_id, image, digests_map)`` is
the caller-side hook: pass a JSON map keyed by instance_id loaded from
``<run_dir>/image_digests.json``; if the map carries an entry for this
instance, return the digest-pinned form instead of the tag form.

Anti-benchmaxxing
-----------------
The pattern is ``starryzhang/sweb.eval.x86_64.{owner}_*_{repo}-{pull_number}``.
The wildcard is structural, not magic-numeric. The caller injects the docker
glob function so the same resolver works in production code, in tests
(callable list), and in dry-run modes.

Stdlib only.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Callable, Iterable, Optional

logger = logging.getLogger("groundtruth.scripts.swebench.image_name_resolver")

_DOCKER_BIN_DEFAULT = "docker"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_image_name(
    instance_id: str,
    dataset_row: dict,
    *,
    docker_glob_fn: Callable[[str], list[str]] | None = None,
) -> str | None:
    """Resolve the Docker image name for a SWE-Bench-Live instance.

    Resolution order:

      1. ``dataset_row["image_name"]`` — direct hit, use as-is.
      2. ``dataset_row["repo"]`` + ``dataset_row["pull_number"]`` →
         build the SWE-Bench-Live tag pattern with the SWE-Bench-Live tag
         slot left as a wildcard, query Docker via ``docker_glob_fn``, pick
         the lexicographically last match.
      3. None — caller treats as a hard failure for this instance.

    Parameters
    ----------
    instance_id : str
        Used only for log lines.
    dataset_row : dict
        SWE-Bench-Live datasets row. Required keys for fallback path:
        ``repo`` (e.g. ``"kozea/WeasyPrint"``), ``pull_number`` (int or str).
    docker_glob_fn : callable, optional
        ``fn(reference_pattern: str) -> list[str]``. Default shells out to
        ``docker images --filter reference=<pattern> --format {{.Repository}}:{{.Tag}}``.
        Tests inject a list-returning callable to avoid Docker dependency.

    Returns
    -------
    str | None
    """
    # 1) Direct from dataset row
    if isinstance(dataset_row, dict):
        direct = dataset_row.get("image_name")
        if direct and isinstance(direct, str):
            # RC-17 (F-002): refuse a literal ``:latest`` tag — it is a
            # floating reference. The caller must either pin a digest
            # via ``apply_digest_overrides`` (run-recorded image_digests.json)
            # or pin a versioned tag in the dataset row itself.
            if direct.endswith(":latest") or direct.rstrip().endswith(":latest"):
                raise ValueError(
                    f"image_name resolver: instance={instance_id} carries "
                    f"floating tag {direct!r}; pin a digest "
                    "(<run_dir>/image_digests.json) or a versioned tag. "
                    "RC-17 / F-002 enforces no :latest in the resolution path."
                )
            logger.debug(
                "image_name resolver: direct hit for %s -> %s",
                instance_id, direct,
            )
            return direct

    # 2) Build pattern from repo + pull_number and glob
    repo = dataset_row.get("repo") if isinstance(dataset_row, dict) else None
    pull_number = (
        dataset_row.get("pull_number") if isinstance(dataset_row, dict) else None
    )
    if not repo or pull_number is None:
        logger.warning(
            "image_name resolver: instance=%s missing repo or pull_number "
            "in dataset row (have keys=%r)",
            instance_id,
            list(dataset_row.keys()) if isinstance(dataset_row, dict) else None,
        )
        return None

    if "/" not in str(repo):
        logger.warning(
            "image_name resolver: instance=%s repo=%r is not in 'owner/name' form",
            instance_id, repo,
        )
        return None
    owner, name = str(repo).split("/", 1)

    # SWE-Bench-Live image naming convention. The tag slot ('*') is the
    # SWE-Bench-Live release tag (a small zero-padded integer like 1776).
    # Caller-injected glob fn does the actual matching — production-safe.
    pattern = (
        f"starryzhang/sweb.eval.x86_64.{owner}_*_{name}-{pull_number}"
    )

    glob_fn = docker_glob_fn if docker_glob_fn is not None else _default_docker_glob
    try:
        matches = list(glob_fn(pattern))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "image_name resolver: glob_fn raised for %s (pattern=%r): %s",
            instance_id, pattern, exc,
        )
        return None

    matches = [m for m in matches if isinstance(m, str) and m.strip()]
    if not matches:
        logger.warning(
            "image_name resolver: no Docker image matched pattern %r for instance=%s",
            pattern, instance_id,
        )
        return None

    # Stable selection: lexicographically last (zero-padded numeric tags
    # sort the same as numeric ascending).
    chosen = sorted(matches)[-1]
    logger.info(
        "image_name resolver: instance=%s pattern=%r matches=%d -> %s",
        instance_id, pattern, len(matches), chosen,
    )
    return chosen


# ---------------------------------------------------------------------------
# RC-17 (F-002): digest capture + override hooks
# ---------------------------------------------------------------------------

def capture_image_digest(
    image: str,
    *,
    docker_inspect_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Return ``"<image-without-tag>@sha256:<digest>"`` for a local image.

    The returned form is content-addressable and stable across registry
    re-tags. Returns ``None`` if Docker has no such image, the inspect
    call fails, or the returned ID is not a sha256.

    ``docker_inspect_fn`` is injectable for tests (returns the raw
    ``{{.Id}}`` string or ``None``). Production path shells out to
    ``docker inspect``.
    """
    if not image or not isinstance(image, str):
        return None
    fn = docker_inspect_fn if docker_inspect_fn is not None else _default_docker_inspect_id
    try:
        raw = fn(image)
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture_image_digest: inspect_fn raised for %s: %s", image, exc)
        return None
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw.startswith("sha256:"):
        # docker inspect on a content-trust-disabled local image returns
        # the local sha256 ID; if anything else comes back we refuse it.
        logger.warning(
            "capture_image_digest: %s returned non-sha256 ID %r — refusing",
            image, raw,
        )
        return None
    # Strip the tag (if any) so the digest pin is unambiguous.
    base = image.split("@", 1)[0]
    if ":" in base.rsplit("/", 1)[-1]:
        # Has a tag — drop it.
        last_slash = base.rfind("/")
        last_colon = base.rfind(":")
        if last_colon > last_slash:
            base = base[:last_colon]
    return f"{base}@{raw}"


def apply_digest_overrides(
    instance_id: str,
    image: Optional[str],
    digests_map: Optional[dict],
) -> Optional[str]:
    """If ``digests_map`` carries an entry for ``instance_id``, return that
    digest-pinned image; otherwise return ``image`` unchanged.

    ``digests_map`` is the parsed contents of
    ``<run_dir>/image_digests.json`` — a flat dict ``{instance_id: pinned}``.
    """
    if not isinstance(digests_map, dict):
        return image
    pinned = digests_map.get(instance_id)
    if pinned and isinstance(pinned, str) and "@sha256:" in pinned:
        logger.info(
            "image_name resolver: instance=%s using pinned digest %s "
            "(override of %r)", instance_id, pinned, image,
        )
        return pinned
    return image


def _default_docker_inspect_id(image: str) -> Optional[str]:
    """Production ``docker inspect --format '{{.Id}}' <image>`` shim."""
    cmd = [_DOCKER_BIN_DEFAULT, "inspect", "--format", "{{.Id}}", image]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("docker inspect failed (%s): %s", " ".join(cmd), exc)
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


# ---------------------------------------------------------------------------
# Default docker glob (production)
# ---------------------------------------------------------------------------

def _default_docker_glob(reference_pattern: str) -> list[str]:
    """Shell out to ``docker images --filter reference=<pattern>``.

    Returns ``["repo:tag", ...]``. Empty list on any failure.
    """
    cmd = [
        _DOCKER_BIN_DEFAULT,
        "images",
        "--filter",
        f"reference={reference_pattern}",
        "--format",
        "{{.Repository}}:{{.Tag}}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("docker images call failed (%s): %s", " ".join(shlex.quote(c) for c in cmd), exc)
        return []

    if proc.returncode != 0:
        # Try with sudo as a fallback (production VMs sometimes require it).
        sudo_cmd = ["sudo", "-n", *cmd]
        try:
            proc = subprocess.run(
                sudo_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []

    out = (proc.stdout or "").splitlines()
    return [line.strip() for line in out if line.strip()]


# ---------------------------------------------------------------------------
# In-module unit tests (smoke harness)
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    failures = 0

    # Test 1: direct image_name hit short-circuits.
    row = {"image_name": "myorg/myimg:1.2.3", "repo": "x/y", "pull_number": 5}
    got = resolve_image_name("inst-1", row, docker_glob_fn=lambda _p: [])
    if got != "myorg/myimg:1.2.3":
        print(f"FAIL test1: expected myorg/myimg:1.2.3 got {got!r}")
        failures += 1
    else:
        print("PASS test1: direct image_name hit")

    # Test 2: pattern glob, single match.
    fake_images = [
        "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:latest",
    ]
    got = resolve_image_name(
        "kozea__weasyprint-2300",
        {"repo": "kozea/weasyprint", "pull_number": 2300},
        docker_glob_fn=lambda _p: fake_images,
    )
    if got != fake_images[0]:
        print(f"FAIL test2: expected {fake_images[0]!r} got {got!r}")
        failures += 1
    else:
        print("PASS test2: single pattern match")

    # Test 3: multiple matches, picks lexicographically last.
    fake_images = [
        "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:latest",
        "starryzhang/sweb.eval.x86_64.kozea_1900_weasyprint-2300:latest",
        "starryzhang/sweb.eval.x86_64.kozea_1850_weasyprint-2300:latest",
    ]
    got = resolve_image_name(
        "kozea__weasyprint-2300",
        {"repo": "kozea/weasyprint", "pull_number": 2300},
        docker_glob_fn=lambda _p: fake_images,
    )
    expected = "starryzhang/sweb.eval.x86_64.kozea_1900_weasyprint-2300:latest"
    if got != expected:
        print(f"FAIL test3: expected {expected!r} got {got!r}")
        failures += 1
    else:
        print("PASS test3: lexicographic tiebreak picks latest tag")

    # Test 4: zero matches → None.
    got = resolve_image_name(
        "missing-1",
        {"repo": "foo/bar", "pull_number": 999},
        docker_glob_fn=lambda _p: [],
    )
    if got is not None:
        print(f"FAIL test4: expected None got {got!r}")
        failures += 1
    else:
        print("PASS test4: empty match returns None")

    # Test 5: missing repo / pull_number → None.
    got = resolve_image_name("bad-1", {"foo": "bar"}, docker_glob_fn=lambda _p: ["x:1"])
    if got is not None:
        print(f"FAIL test5: expected None got {got!r}")
        failures += 1
    else:
        print("PASS test5: missing repo/pull_number returns None")

    # Test 6: malformed repo (no slash) → None.
    got = resolve_image_name(
        "bad-2",
        {"repo": "norepo", "pull_number": 1},
        docker_glob_fn=lambda _p: ["x:1"],
    )
    if got is not None:
        print(f"FAIL test6: expected None got {got!r}")
        failures += 1
    else:
        print("PASS test6: malformed repo returns None")

    # Test 7: docker_glob_fn raising is tolerated → None.
    def _raises(_p: str) -> list[str]:
        raise RuntimeError("docker socket down")
    got = resolve_image_name(
        "err-1",
        {"repo": "kozea/weasyprint", "pull_number": 2300},
        docker_glob_fn=_raises,
    )
    if got is not None:
        print(f"FAIL test7: expected None got {got!r}")
        failures += 1
    else:
        print("PASS test7: glob_fn exception returns None")

    # Test 8 (RC-17 / F-002): :latest in image_name raises ValueError.
    try:
        resolve_image_name(
            "floating-1",
            {"image_name": "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:latest"},
            docker_glob_fn=lambda _p: [],
        )
        print("FAIL test8: expected ValueError on :latest, got pass-through")
        failures += 1
    except ValueError:
        print("PASS test8: :latest in image_name raises ValueError")

    # Test 9 (RC-17 / F-002): capture_image_digest returns digest-pinned form.
    pinned = capture_image_digest(
        "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:1.0",
        docker_inspect_fn=lambda _i: "sha256:abc123def456",
    )
    expected = (
        "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300@sha256:abc123def456"
    )
    if pinned != expected:
        print(f"FAIL test9: expected {expected!r} got {pinned!r}")
        failures += 1
    else:
        print("PASS test9: capture_image_digest strips tag and pins digest")

    # Test 10 (RC-17 / F-002): apply_digest_overrides routes by instance_id.
    digests_map = {"k__w-2300": "starryzhang/sweb.eval.x86_64.k_w@sha256:deadbeef"}
    out = apply_digest_overrides("k__w-2300", "starryzhang/sweb.eval.x86_64.k_w:tag1", digests_map)
    if out != digests_map["k__w-2300"]:
        print(f"FAIL test10: expected {digests_map['k__w-2300']!r} got {out!r}")
        failures += 1
    else:
        print("PASS test10: apply_digest_overrides honors digest map")

    # Test 11: apply_digest_overrides passes through when no override present.
    out = apply_digest_overrides("missing", "x:1", {"other": "z@sha256:abc"})
    if out != "x:1":
        print(f"FAIL test11: expected 'x:1' got {out!r}")
        failures += 1
    else:
        print("PASS test11: apply_digest_overrides passthrough on miss")

    return failures


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    rc = _run_self_tests()
    print(f"\nresolve_image_name: {11 - rc}/11 passed")
    sys.exit(0 if rc == 0 else 1)
