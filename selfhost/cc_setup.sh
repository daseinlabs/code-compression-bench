#!/usr/bin/env bash
set +e
echo "=== SETUP START $(date -u) ==="
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y >/dev/null 2>&1
sudo apt-get install -y git python3-venv python3-pip build-essential curl ca-certificates jq >/dev/null 2>&1
echo "[ok] apt base"
echo "=== docker ==="
sudo apt-get install -y docker.io >/dev/null 2>&1
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
docker --version
echo "=== node 22 ==="
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null 2>&1
sudo apt-get install -y nodejs >/dev/null 2>&1
echo "node $(node -v)  npm $(npm -v)"
echo "=== claude code CLI ==="
sudo npm install -g @anthropic-ai/claude-code >/dev/null 2>&1
echo "claude $(claude --version 2>&1 | head -1)"
echo "=== bench repo + venv ==="
cd ~
[ -d code-compression-bench ] || git clone -q https://github.com/daseinlabs/code-compression-bench.git
cd ~/code-compression-bench
python3 -m venv .venv
.venv/bin/pip install -q -U pip wheel
.venv/bin/pip install -q -e . 2>&1 | tail -2
.venv/bin/pip install -q claude-agent-sdk litellm google-cloud-aiplatform vertexai google-auth swebench 2>&1 | tail -3
echo "=== woz plugin (best-effort) ==="
cd ~
(git clone -q https://github.com/WithWoz/wozcode-plugin.git 2>/dev/null && cd wozcode-plugin && npm install >/dev/null 2>&1 && echo "woz cloned") || echo "woz clone skipped (resolve at woz smoke)"
echo "=== rtk binary (hook arm; system-PATH for the non-interactive runner) ==="
# rtk arm is a hook, not a proxy: it needs the real rtk binary resolvable by the
# NON-INTERACTIVE runner process. Publish it onto /usr/local/bin (already on the
# stock PATH) so `gcloud ... ssh --command` (no .bashrc) resolves it. Idempotent.
if [ -x "$HOME/.local/bin/rtk" ]; then
  sudo ln -sfn "$HOME/.local/bin/rtk" /usr/local/bin/rtk
  echo "rtk $(/usr/local/bin/rtk --version 2>&1 | head -1) -> /usr/local/bin/rtk"
elif [ -x ~/code-compression-bench/selfhost/rtk/install.sh ]; then
  RTK_INSTALL=1 bash ~/code-compression-bench/selfhost/rtk/install.sh || echo "[rtk] provisioning skipped (resolve at rtk smoke)"
else
  echo "[rtk] no rtk binary and no installer found (resolve at rtk smoke)"
fi
echo "=== VERIFY ==="
node -v
~/code-compression-bench/.venv/bin/python -c "import claude_agent_sdk, litellm; print('py imports: claude_agent_sdk + litellm OK')" 2>&1 | tail -1
echo "=== SETUP DONE $(date -u) ==="
