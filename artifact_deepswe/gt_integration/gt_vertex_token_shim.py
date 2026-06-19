# gt_vertex_token_shim.py — in-container litellm Vertex auth shim (NO ADC / metadata / SA-key).
#
# WHY THIS EXISTS
# DeepSWE tasks set allow_internet=false, so pier runs the agent inside an
# internal-only docker network behind a squid egress proxy whose allowlist is
# ".googleapis.com" only (pier/agents/installed/mini_swe_agent.py
# _DEFAULT_PROVIDER_DOMAINS["vertex_ai"]). Inside that container:
#   - the GCE metadata server (169.254.169.254 / metadata.google.internal) is
#     UNREACHABLE -> google.auth ADC cannot mint a token,
#   - org policy iam.disableServiceAccountKeyCreation forbids SA-key JSON,
#   - BUT aiplatform.googleapis.com and oauth2.googleapis.com ARE reachable
#     through the proxy (they match ".googleapis.com").
# So we forward a PRE-MINTED OAuth access token: the HOST mints it from the
# metadata server (the host has cloud-platform scope) and keeps a fresh copy at
# GT_VERTEX_TOKEN_FILE, bind-mounted read-only via the /gt_auth DIRECTORY so a
# host-side refresh is visible live inside the running container (a single-file
# bind mount pins the inode and would NOT see refreshes — the directory mount does).
#
# WHAT IT DOES
# Patches litellm's VertexBase._ensure_access_token / .get_access_token to return
# the forwarded token directly, skipping ALL google-auth credential loading and
# refresh. The token is re-read from the file on EVERY call, so a refresh (host
# re-mints every ~40 min; token TTL ~60 min) takes effect mid-run with no restart.
#
# LOADING: dropped on the agent's PYTHONPATH (/gt_auth) alongside a sitecustomize.py
# that imports it, so it auto-installs on interpreter start with no edit to
# mini-swe-agent. No-op when GT_VERTEX_TOKEN[_FILE] is absent (ADC path untouched).
import os

_TOKEN_FILE = os.environ.get("GT_VERTEX_TOKEN_FILE", "/gt_auth/vertex_token")


def _read_token():
    t = os.environ.get("GT_VERTEX_TOKEN")
    if t and t.strip():
        return t.strip()
    try:
        with open(_TOKEN_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _project():
    return (
        os.environ.get("VERTEXAI_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("VERTEX_PROJECT")
        or ""
    )


def _install():
    if not _read_token():
        return  # no forwarded token -> leave litellm's default ADC path untouched
    try:
        from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
    except Exception:
        return

    def _ensure_access_token(self, credentials, project_id, custom_llm_provider):
        return _read_token(), (project_id or _project())

    def get_access_token(self, credentials, project_id, _retry_reauth=False):
        return _read_token(), (project_id or _project())

    VertexBase._ensure_access_token = _ensure_access_token
    VertexBase.get_access_token = get_access_token


_install()
