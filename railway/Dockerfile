# Railway single-container image: OpenHands 0.54 (local runtime) + GroundTruth +
# beets checked out at the buggy base commit. NO Docker-in-Docker.
#
# Build context = the GT repo root (railway up is run from D:\Groundtruth).
# .railwayignore keeps the upload small.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- system deps: git (clone), build-essential+gcc (CGO for gt-index, native
#     wheels), curl (DeepSeek auth probe), ca-certificates ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        git build-essential gcc curl ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*

# --- Go toolchain (to build the gt-index binary at image-build time so graph.db
#     can be produced without shipping a prebuilt blob). Pinned to match
#     .github/actions/setup-eval (go 1.22). ---
ARG GO_VERSION=1.22.10
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
        -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}" \
    CGO_ENABLED=1

WORKDIR /app

# ---------------------------------------------------------------------------
# 1. OpenHands 0.54.0 from SOURCE (the `evaluation.*` modules + the 0.54
#    runtime registry with LocalRuntime live in the monorepo, NOT the
#    openhands-ai pip wheel). Same source the GHA setup-eval action clones.
#    pip install -e . so `import openhands` + `from evaluation... import ...`
#    both resolve. PYTHONPATH below also points at /opt/OpenHands so the
#    `evaluation` namespace package is importable.
# ---------------------------------------------------------------------------
RUN git clone --depth 1 --branch 0.54.0 \
        https://github.com/All-Hands-AI/OpenHands.git /opt/OpenHands \
    && cd /opt/OpenHands \
    && pip install -e . \
    && pip install datasets toml

# ---------------------------------------------------------------------------
# 2. GroundTruth: copy only what the driver/wrapper need (src/, scripts/,
#    gt-index/, pyproject). .railwayignore already excludes scratch.
# ---------------------------------------------------------------------------
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY gt-index/ /app/gt-index/
RUN pip install -e .

# ---------------------------------------------------------------------------
# 3. Build the gt-index Go binary (CGO -> go-sqlite3). If this fails the
#    image still builds; the entrypoint will skip graph.db rebuild and the
#    baked db (if any) is used. We do NOT fail the whole image on a Go hiccup.
# ---------------------------------------------------------------------------
RUN cd /app/gt-index \
    && (CGO_ENABLED=1 go build -o /usr/local/bin/gt-index ./cmd/gt-index/ \
         && chmod +x /usr/local/bin/gt-index \
         && echo "gt-index built OK") \
    || echo "WARN: gt-index build failed — graph.db must be baked or rebuilt later"

# ---------------------------------------------------------------------------
# 4. beets at the buggy base commit + test deps. pip install -e . of beets so
#    the agent's edits run against an installed package (imports resolve).
#    BASE_COMMIT is the full 40-char SHA from SWE-bench-Live lite for
#    beetbox__beets-5495 (the task's `base_commit` field).
# ---------------------------------------------------------------------------
ARG BEETS_BASE_COMMIT=fa10dcf11add0afd3b4b22af29f8d504e7ef8a0a
ENV GT_INSTANCE_ID=beetbox__beets-5495 \
    GT_REPO=beetbox/beets \
    GT_BASE_COMMIT=${BEETS_BASE_COMMIT} \
    GT_WORKSPACE_ROOT=/workspace
RUN mkdir -p /workspace \
    && git clone https://github.com/beetbox/beets.git \
        /workspace/beetbox__beets-5495 \
    && cd /workspace/beetbox__beets-5495 \
    && git checkout -f ${BEETS_BASE_COMMIT} \
    && git reset --hard ${BEETS_BASE_COMMIT} \
    && for r in $(git remote); do git remote remove "$r"; done \
    && (pip install -e . || pip install -e '.[test]' || echo "WARN: beets editable install partial") \
    && pip install responses pytest pytest-cov || true

# ---------------------------------------------------------------------------
# 5. graph.db strategy = BUILD AT IMAGE-BUILD TIME with the gt-index binary.
#    The beets repo is checked out above; if gt-index built, index it now and
#    bake graph.db into the image at /workspace/beetbox__beets-5495/graph.db.
#    Reproducible, no Docker-in-Docker, no blob upload. If gt-index is absent,
#    leave a marker; the entrypoint retries at start, else a prebuilt graph.db
#    can be COPY'd in (see DEPLOY.md).
# ---------------------------------------------------------------------------
RUN if [ -x /usr/local/bin/gt-index ]; then \
        /usr/local/bin/gt-index -root /workspace/beetbox__beets-5495 \
            -output /workspace/beetbox__beets-5495/graph.db \
        && echo "graph.db baked OK" \
        || echo "WARN: gt-index run failed at build time"; \
    else \
        echo "WARN: no gt-index binary; graph.db not baked"; \
    fi

# PYTHONPATH: GT src + GT swebench scripts (oh_gt_full_wrapper) + OpenHands
# source root (so `evaluation` namespace pkg resolves) + OH pkg.
ENV PYTHONPATH="/app/src:/app/scripts/swebench:/opt/OpenHands" \
    GT_INDEX_BINARY="/usr/local/bin/gt-index" \
    GT_OUT_DIR="/workspace/results" \
    GT_DEBUG_DIR="/tmp/gt_debug" \
    GT_CONFIG_TOML="/app/railway/config.toml" \
    RUNTIME="local"

# Do NOT bake any API key. DEEPSEEK_API_KEY is injected by Railway at runtime.

# tmux: OH 0.54 LocalRuntime's bash session uses libtmux, which shells out to the
# `tmux` binary — absent from python:3.12-slim, causing
# libtmux.exc.TmuxCommandNotFound -> "Application startup failed". Installed in a
# LATE layer so the expensive OH/gt-index/beets/graph.db layers above stay cached.
RUN apt-get update && apt-get install -y --no-install-recommends tmux \
    && rm -rf /var/lib/apt/lists/*

# railway/ (driver + entrypoint + config) COPY'd LAST so editing the driver is a
# fast cached rebuild — the expensive OH/gt-index/beets/graph.db layers above stay
# cached and only this small COPY + chmod re-run on a run_one_task.py change.
COPY railway/ /app/railway/
RUN chmod +x /app/railway/entrypoint.sh

# OH 0.54 LocalRuntime's action-execution server REFUSES to run as root: it calls
# `useradd openhands` with the current UID, and as root that UID is 0 ->
# "useradd: UID 0 is not unique" -> "Application startup failed. Exiting." So run
# the container as a non-root user that owns the workspace it must edit. The
# graph.db + beets repo + /app/results all live under /workspace, chowned here.
RUN useradd -m -u 1000 -s /bin/bash openhands \
    && mkdir -p /workspace/results \
    && chown -R openhands:openhands /workspace
USER openhands
ENTRYPOINT ["/app/railway/entrypoint.sh"]
