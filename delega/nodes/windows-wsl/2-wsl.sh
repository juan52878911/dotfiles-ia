#!/usr/bin/env bash
# Step 2 · inside the Ubuntu (WSL) of the PC, as your normal user (asks for sudo):
#   cp .env.example .env && $EDITOR .env && bash 2-wsl.sh
# Idempotent. Leaves: systemd, key-only sshd, git/python3/docker, Claude Code and, optionally,
# a WireGuard tunnel to a hub. All site-specific values come from .env (or the environment).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$HERE/.env"
  set +a
fi
ORCH_KEY="${ORCH_SSH_PUBKEY:?set ORCH_SSH_PUBKEY in .env (public SSH key of the orchestrator)}"
GIT_USER_NAME="${GIT_USER_NAME:?set GIT_USER_NAME in .env}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:?set GIT_USER_EMAIL in .env}"
EXPECT_EMAIL_DOMAIN="${EXPECT_EMAIL_DOMAIN:?set EXPECT_EMAIL_DOMAIN in .env}"
WG_IP="${WG_IP:-}"
HUB_PUB="${HUB_PUB:-}"
HUB_ENDPOINT="${HUB_ENDPOINT:-}"

# --- systemd (sshd and wg-quick need it as services) --------------------------
# Keeps what /etc/wsl.conf already has; only adds systemd=true.
if ! grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null; then
  sudo touch /etc/wsl.conf
  sudo cp /etc/wsl.conf /etc/wsl.conf.before-delega
  if grep -q '^\[boot\]' /etc/wsl.conf; then
    sudo sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
  else
    printf '\n[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
  fi
  echo ">> systemd enabled. In PowerShell: wsl --shutdown ; open Ubuntu again and re-run this script."
  exit 0
fi

# --- Packages ------------------------------------------------------------------
sudo apt-get update -qq
sudo apt-get install -y -qq openssh-server git python3 curl ca-certificates jq
if ! command -v docker >/dev/null; then       # optional: only needed for jobs that use containers
  sudo apt-get install -y -qq docker.io docker-compose-v2
  sudo systemctl enable --now docker
fi
sudo usermod -aG docker "$USER"

# --- SSH, key only -------------------------------------------------------------
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

# --- WireGuard to a hub (optional): the node keeps $WG_IP wherever it is -------
# Only WG_ALLOWED goes through the tunnel (not the home LAN, so it is not shadowed).
WG_PUB=""
if [[ -n "$WG_IP" ]]; then
  : "${HUB_PUB:?set HUB_PUB in .env}" "${HUB_ENDPOINT:?set HUB_ENDPOINT in .env}"
  sudo apt-get install -y -qq wireguard-tools
  if ! sudo test -f /etc/wireguard/wg0.conf; then
    sudo install -d -m 700 /etc/wireguard
    priv=$(wg genkey)
    sudo tee /etc/wireguard/wg0.conf >/dev/null <<CONF
[Interface]
Address = $WG_IP/32
PrivateKey = $priv
MTU = 1420

[Peer]
PublicKey = $HUB_PUB
Endpoint = $HUB_ENDPOINT
AllowedIPs = ${WG_ALLOWED:-10.10.10.0/24}
PersistentKeepalive = 25
CONF
    sudo chmod 600 /etc/wireguard/wg0.conf
    unset priv
  fi
  sudo systemctl enable --now wg-quick@wg0
  WG_PUB=$(sudo wg show wg0 public-key)
fi

# --- Claude Code: download the installer, then run it (never piped straight into a shell) ---
if ! command -v claude >/dev/null; then
  installer=$(mktemp)
  curl -fsSL https://claude.ai/install.sh -o "$installer"
  bash "$installer"
  rm -f "$installer"
fi
grep -q '.local/bin' ~/.profile || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
export PATH="$HOME/.local/bin:$PATH"
git config --global user.name >/dev/null || git config --global user.name "$GIT_USER_NAME"
git config --global user.email >/dev/null || git config --global user.email "$GIT_USER_EMAIL"
mkdir -p "$HOME/${DELEGA_WORKDIR:-delega}/jobs"

account=$(claude auth status 2>/dev/null | jq -r '.email // empty' || true)
echo
echo "================ SUMMARY (for the node entry in your delega config) ================"
echo "user=$USER  wg_ip=${WG_IP:-none}  wg_pubkey=${WG_PUB:-none}"
echo "ram_total=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)MB  cpus=$(nproc)"
echo "claude=$(claude --version 2>/dev/null)  account=${account:-NOT SIGNED IN}"
[[ "$account" == *@"$EXPECT_EMAIL_DOMAIN" ]] || echo "TODO: run 'claude', /login with the account assigned to you (@$EXPECT_EMAIL_DOMAIN) and exit."
