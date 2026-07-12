"""Pin the SM-9c cross-session store MOUNT+ENV into the live-mini (Live-Lite) workflow.

BUG-2 (2026-07-12): the durable per-repo causal-consumption ledger (xsession_memory)
is READ by the mini seam (``gt_mini_patch._xsession_ensure_loaded`` /
``_xsession_flush``) behind ``$GT_XSESSION_DIR`` — but NOTHING in the live workflow set
that env or provided a WRITABLE durable dir, so the two memory capabilities
(suppress-already-inert + rank-up-consumed) silently no-op on every paid run.

``swebench_live_lite_full.yml`` runs the agent via a direct ``docker run`` (no pier),
so — unlike ``deepswe_full.yml`` (pier + ``--ae`` via ``gt_ae_block.sh``) — the store
dir must be a SEPARATE writable bind-mount and ``GT_XSESSION_DIR`` forwarded on the
``docker run -e`` line, not via ``--ae``. (The pier path is wired in ``gt_ae_block.sh``
and guarded by ``test_r1_ae_parity_invariant_failclosed``.)

These are DETERMINISTIC staging pins (read the workflow + the consumer, assert the
in-container path the workflow forwards on the env equals the path it mounts writable).
They mirror ``tests/test_live_lite_l6_staging_pin_20260712.py`` but for the SM-9c store.
No live run — Stage-1 correctness only.

RED-first: on the pre-fix tree these FAIL (no ``GT_XSESSION_DIR`` / ``HOST_GT_XSESSION``
anywhere in the mini workflow). Mutation: dropping the ``-v ...:rw`` mount reddens
``test_workflow_mounts_xsession_dir_rw``.
"""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_CONSUMER = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

# The store's writable host dir + its in-container mount target. A SEPARATE small RW
# mount (kept off the telemetry-sink /gt_out mount) so a later cross-RUN artifact
# upload (option (b)) can scope to exactly this dir.
_HOST_XSESSION = "/tmp/gt_xsession"
_CONTAINER_XSESSION = "/opt/gt_xsession"


def _wf() -> str:
    return _WF.read_text(encoding="utf-8")


def test_workflow_is_valid_yaml():
    """Editing the staging must not corrupt the workflow document."""
    doc = yaml.safe_load(_wf())
    assert isinstance(doc, dict) and doc, "mini workflow did not parse to a mapping"


def test_workflow_creates_writable_xsession_host_dir():
    """The host store dir must be created + world-writable (mirrors HOST_GT_OUT), so the
    bind-mount is writable for the in-container store dump."""
    wf = _wf()
    assert f"HOST_GT_XSESSION={_HOST_XSESSION}" in wf, "HOST_GT_XSESSION host dir not defined"
    assert 'mkdir -p "${HOST_GT_XSESSION}"' in wf, "HOST_GT_XSESSION not created (mkdir -p)"
    assert 'chmod 777 "${HOST_GT_XSESSION}"' in wf, "HOST_GT_XSESSION not made writable (chmod)"


def test_workflow_mounts_xsession_dir_rw():
    """A SEPARATE writable bind-mount for the store dir (the store needs WRITE access, so
    it cannot ride the /opt/gt:ro inject mount). Dropping this line is the mutation."""
    wf = _wf()
    assert f'-v "${{HOST_GT_XSESSION}}:{_CONTAINER_XSESSION}:rw"' in wf, (
        "SM-9c writable xsession bind-mount missing/renamed on docker run"
    )


def test_workflow_forwards_xsession_dir_env():
    """GT_XSESSION_DIR must be forwarded to the container via `docker run -e` (the direct
    docker path forwards path-valued env this way; it is NOT a profile member so the
    in-container rl_profile fan-out never sets it)."""
    wf = _wf()
    assert f'-e GT_XSESSION_DIR="{_CONTAINER_XSESSION}"' in wf, "GT_XSESSION_DIR not forwarded on docker run"


def test_env_path_equals_mount_target():
    """The in-container path the workflow forwards on GT_XSESSION_DIR MUST equal the path
    it mounts writable — so the seam writes into the durable, host-backed dir (not an
    ephemeral container path). Both are derived from the single _CONTAINER_XSESSION const,
    and each is independently asserted present, so drift in either surface trips a pin."""
    wf = _wf()
    mount = f'-v "${{HOST_GT_XSESSION}}:{_CONTAINER_XSESSION}:rw"'
    env = f'-e GT_XSESSION_DIR="{_CONTAINER_XSESSION}"'
    assert mount in wf and env in wf, "mount target and forwarded env must share the container path"


def test_consumer_reads_forwarded_env_key():
    """Close the loop: the seam must actually read GT_XSESSION_DIR (the var the workflow
    forwards) to load/persist the store. Reads the consumer source directly."""
    src = _CONSUMER.read_text(encoding="utf-8")
    assert 'os.environ.get("GT_XSESSION_DIR"' in src, "consumer no longer reads GT_XSESSION_DIR"
