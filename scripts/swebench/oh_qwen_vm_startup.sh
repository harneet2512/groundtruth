#!/bin/bash
# Startup script for gt-runner-oh-qwen --- fires once on first boot.
# Installs docker + python3.12 + poetry, clones the SWE-bench-Live/OpenHands fork at
# f4da691c, the microsoft/SWE-bench-Live evaluator on the python-only branch, writes
# the OH LLM config block, and leaves a sentinel at /var/log/oh_startup_done.
#
# Logs to /var/log/oh_startup.log. Tail with: `sudo tail -f /var/log/oh_startup.log`.

set -eux
exec > >(tee -a /var/log/oh_startup.log) 2>&1
echo "=== $(date -u) :: oh_qwen_vm_startup begin ==="

TARGET_USER=ubuntu
TARGET_HOME=/home/$TARGET_USER

# ---------- 1. System packages (docker, python3.12, git, jq, build) ----------
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release git jq build-essential \
    python3.12 python3.12-venv python3.12-dev python3-pip pipx

# Docker engine
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
codename=$(lsb_release -cs)
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $codename stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker "$TARGET_USER"
systemctl enable --now docker

# ---------- 2. Poetry for the ubuntu user ----------
sudo -u "$TARGET_USER" bash -lc 'curl -sSL https://install.python-poetry.org | python3.12 -'
echo 'export PATH="$HOME/.local/bin:$PATH"' | sudo -u "$TARGET_USER" tee -a "$TARGET_HOME/.bashrc" >/dev/null

# ---------- 3. Clone SWE-bench-Live/OpenHands @ f4da691c + poetry install ----------
sudo -u "$TARGET_USER" bash -lc '
set -eux
cd $HOME
if [ ! -d OpenHands ]; then
    git clone https://github.com/SWE-bench-Live/OpenHands.git
fi
cd OpenHands
git fetch --all
git checkout f4da691c
$HOME/.local/bin/poetry env use python3.12
$HOME/.local/bin/poetry install --with dev --no-interaction --no-root || \
$HOME/.local/bin/poetry install --no-interaction --no-root
'

# ---------- 4. Write $OH/config.toml with [llm.qwen3_coder_vertex] ----------
sudo -u "$TARGET_USER" bash -lc 'cat > $HOME/OpenHands/config.toml' <<"CFG"
[core]
workspace_base = "/tmp/workspace"

[llm.qwen3_coder_vertex]
model = "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
temperature = 0.0
top_p = 1.0
max_output_tokens = 8192
native_tool_calling = true
caching_prompt = false
drop_params = true
num_retries = 5
timeout = 300
CFG

# ---------- 5. Clone microsoft/SWE-bench-Live @ python-only + pip install -e ----------
sudo -u "$TARGET_USER" bash -lc '
set -eux
cd $HOME
if [ ! -d SWE-bench-Live ]; then
    git clone https://github.com/microsoft/SWE-bench-Live.git
fi
cd SWE-bench-Live
git fetch --all
git checkout python-only
python3.12 -m venv $HOME/swebench-live-venv
source $HOME/swebench-live-venv/bin/activate
pip install -U pip wheel
pip install -e .
'

# ---------- 6. Preflight venv (litellm + datasets, separate from OH poetry env) ----------
sudo -u "$TARGET_USER" bash -lc '
set -eux
python3.12 -m venv $HOME/preflight-venv
source $HOME/preflight-venv/bin/activate
pip install -U pip wheel
# RC-17 (F-012): pin litellm so cost-rounding + Vertex auth + streaming
# parsing do not drift mid-phase. The smoke runner records pip show litellm
# to versions.json so any bump is visible in the run artifact.
pip install "litellm==1.50.4" datasets google-auth google-cloud-aiplatform
pip show litellm
'

# ---------- 7. Workspace dirs ----------
sudo -u "$TARGET_USER" mkdir -p "$TARGET_HOME/cal20" "$TARGET_HOME/cal20/outdir"

echo "=== $(date -u) :: oh_qwen_vm_startup done ==="
touch /var/log/oh_startup_done
