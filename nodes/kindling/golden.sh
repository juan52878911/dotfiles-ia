#!/usr/bin/env bash
# Bakes a kindling golden image for delega's `runner = "kindling"` nodes: a node22 microVM with
# git, python3, a pinned Claude Code, the profile's node bundle (`delega sync --tar`) and the repo
# checked out in ~/<vm_repo> at branch <golden_base_ref>. NO credentials: the worker's token enters
# each clone at run time, by process.
#
#   nodes/kindling/golden.sh <local-repo> [golden-name]
#
# KINDLING_HOST (required): ssh alias of the kindling host · DELEGA_PROFILE: profile of the bundle
# CLAUDE_VERSION (required): the version your other nodes run · GIT_EMAIL / GIT_NAME: worker commits
# VM_USER (worker) · VM_REPO (wt) · BASE_REF (delega-base): must match the node config
# EXTRA: optional script run as VM_USER in ~/VM_REPO after the base setup (install dependencies…)
# APT_EXTRA: more apt packages for the image (e.g. "build-essential libpq-dev")
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "${1:?usage: golden.sh <local-repo> [golden-name]}" && pwd)
NAME=${2:-claude-$(basename "$(dirname "$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)")" | tr 'A-Z_' 'a-z-')}
KHOST=${KINDLING_HOST:?set KINDLING_HOST}
: "${CLAUDE_VERSION:?set CLAUDE_VERSION}"
VM=golden-$$
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5 "$KHOST")

tmp=$(mktemp -d); trap 'rm -rf "$tmp"; "${SSH[@]}" "rm -rf /tmp/$VM" || true' EXIT
echo "== rules and repo ($(git -C "$REPO" rev-parse --short HEAD))"
delega sync ${DELEGA_PROFILE:+--profile "$DELEGA_PROFILE"} --tar "$tmp/rules.tar"
git -C "$REPO" bundle create -q "$tmp/repo.bundle" HEAD
# kling cp moves at most 64 MiB per file: the bundle goes in pieces.
(cd "$tmp" && split -b 48m repo.bundle repo.bundle. && rm repo.bundle)
cp "$HERE/provision.sh" "$tmp/"
[[ -z "${EXTRA:-}" ]] || cp "$EXTRA" "$tmp/extra.sh"
"${SSH[@]}" "mkdir -p /tmp/$VM" && scp -q "$tmp"/* "$KHOST:/tmp/$VM/"

echo "== build microVM $VM"
"${SSH[@]}" "kling run -name $VM -image node22 -cpus 4 -mem 4G -disk 16G -egress internet -allow-exec -wait-ready" | tail -1
"${SSH[@]}" "kling exec $VM -- mkdir -p /tmp/in && for f in /tmp/$VM/*; do kling cp \$f $VM:/tmp/in/\$(basename \$f) >/dev/null; done"
q() { printf '%q' "$1"; }
"${SSH[@]}" "kling exec -timeout 50m -e CLAUDE_VERSION=$(q "$CLAUDE_VERSION") \
  -e GIT_EMAIL=$(q "${GIT_EMAIL:-$(git -C "$REPO" config user.email)}") -e GIT_NAME=$(q "${GIT_NAME:-$(git -C "$REPO" config user.name)}") \
  -e VM_USER=$(q "${VM_USER:-worker}") -e VM_REPO=$(q "${VM_REPO:-wt}") -e BASE_REF=$(q "${BASE_REF:-delega-base}") -e APT_EXTRA=$(q "${APT_EXTRA:-}") \
  $VM -- bash /tmp/in/provision.sh"

echo "== golden $NAME"
"${SSH[@]}" "kling save -replace $VM $NAME && kling rm $VM" | tail -2
