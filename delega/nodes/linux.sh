#!/usr/bin/env bash
# Generic delega node bootstrap for a Debian/Ubuntu-like Linux machine (VM, mini PC, cloud box).
#   ORCH_SSH_PUBKEY="ssh-ed25519 AAAA… orchestrator" bash nodes/linux.sh
# Idempotent. Needs sudo. Leaves: key-only sshd, git, python3, Claude Code, the work dir and,
# unless WITH_DOCKER=0, Docker. Nothing site-specific is baked in: values come from the environment.
#   ORCH_SSH_PUBKEY    public key of the orchestrator machine (required)
#   DELEGA_WORKDIR     directory under $HOME (default: delega); must match `workdir` in the node config
#   WITH_DOCKER        1 (default) installs Docker for jobs that need containers, 0 skips it
set -euo pipefail
ORCH_KEY="${ORCH_SSH_PUBKEY:?set ORCH_SSH_PUBKEY (public SSH key of the orchestrator)}"
WORKDIR="${DELEGA_WORKDIR:-delega}"
WITH_DOCKER="${WITH_DOCKER:-1}"

sudo apt-get update -qq
sudo apt-get install -y -qq openssh-server git python3 curl ca-certificates jq
if [[ "$WITH_DOCKER" == 1 ]]; then
  if ! command -v docker >/dev/null; then
    sudo apt-get install -y -qq docker.io docker-compose-v2
    sudo systemctl enable --now docker
  fi
  sudo usermod -aG docker "$USER"
fi

# SSH: key only
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qF "$ORCH_KEY" ~/.ssh/authorized_keys || echo "$ORCH_KEY" >> ~/.ssh/authorized_keys
sudo tee /etc/ssh/sshd_config.d/50-delega.conf >/dev/null <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
ClientAliveInterval 30
CONF
sudo systemctl enable --now ssh
sudo systemctl restart ssh

# Claude Code: download the installer, then run it (never piped straight into a shell)
if ! command -v claude >/dev/null; then
  installer=$(mktemp)
  curl -fsSL https://claude.ai/install.sh -o "$installer"
  bash "$installer"
  rm -f "$installer"
fi
grep -q '.local/bin' ~/.profile || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$HOME/$WORKDIR/jobs"
account=$(claude auth status 2>/dev/null | jq -r '.email // empty' || true)
echo "node ready: user=$USER workdir=~/$WORKDIR claude=$(claude --version 2>/dev/null || echo missing) account=${account:-NOT SIGNED IN}"
[[ -n "$account" ]] || echo "TODO: run 'claude' and /login with the account assigned to you, then: delega doctor"
