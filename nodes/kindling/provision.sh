#!/usr/bin/env bash
# Runs as root INSIDE the build microVM (golden.sh starts it). Input in /tmp/in: repo.bundle.*,
# rules.tar, dbctl.sh, optional extra.sh. Env: CLAUDE_VERSION, GIT_EMAIL, GIT_NAME, VM_USER, VM_REPO,
# BASE_REF, APT_EXTRA (more packages, space separated), DB_ENGINES ("engine[:version] …").
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
# shellcheck disable=SC2086  # APT_EXTRA is a list of packages
apt-get install -y -qq git python3 python3-venv python3-pip jq ca-certificates curl ${APT_EXTRA:-} >/dev/null
npm i -g --silent --no-fund --no-audit "@anthropic-ai/claude-code@$CLAUDE_VERSION"
install -m 755 /tmp/in/dbctl.sh /usr/local/bin/delega-db
for e in ${DB_ENGINES:-}; do
  # shellcheck disable=SC2046  # no version = the distribution's
  delega-db install "${e%%:*}" $([ "$e" = "${e#*:}" ] || echo "${e#*:}")
done
id "$VM_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$VM_USER"
cat /tmp/in/repo.bundle.* > /tmp/in/repo.bundle
chmod -R a+rX /tmp/in

# shellcheck disable=SC2016  # expanded by the worker's shell
runuser -u "$VM_USER" -- env GIT_EMAIL="$GIT_EMAIL" GIT_NAME="$GIT_NAME" VM_REPO="$VM_REPO" BASE_REF="$BASE_REF" \
  bash -euo pipefail -c '
cd ~
git config --global user.name "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"
git init -q "$VM_REPO"
git -C "$VM_REPO" fetch -q /tmp/in/repo.bundle HEAD:refs/heads/"$BASE_REF"
git -C "$VM_REPO" checkout -q "$BASE_REF"
d=$(mktemp -d) && tar -C "$d" -xf /tmp/in/rules.tar --no-same-owner
mkdir -p ~/.claude && cp -R "$d"/claude/. ~/.claude/ && { [ ! -d "$d"/home ] || cp -R "$d"/home/. ~/; }
rm -rf "$d"
if [ -f /tmp/in/extra.sh ]; then (cd ~/"$VM_REPO" && bash -euo pipefail /tmp/in/extra.sh); fi
echo "claude $(claude --version) · $(python3 -V)"
'

# No credentials nor leftovers in the golden (npm and pip caches of root and the worker).
for d in /tmp/in "$HOME/.npm" "/home/$VM_USER/.npm" "/home/$VM_USER/.cache/pip"; do rm -rf "$d"; done
apt-get clean && rm -rf /var/lib/apt/lists/*
test ! -e "/home/$VM_USER/.claude/.credentials.json"
sync
echo "provisioned"
