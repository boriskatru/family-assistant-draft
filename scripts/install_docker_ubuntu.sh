#!/usr/bin/env bash
# Optional preparation for a new Ubuntu VPS. Does not remove existing packages/data.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo: sudo bash scripts/install_docker_ubuntu.sh' >&2
  exit 1
fi
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  echo 'Docker Engine and Compose are already available.'
  exit 0
fi
. /etc/os-release
if [[ ${ID:-} != ubuntu ]]; then
  echo 'This helper supports Ubuntu only; follow docs.docker.com/engine/install/ for this OS.' >&2
  exit 1
fi
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
    echo "Existing package $package requires manual review before installing Docker CE." >&2
    exit 1
  fi
done
apt-get update
apt-get install -y ca-certificates curl python3 unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
arch=$(dpkg --print-architecture)
codename=${UBUNTU_CODENAME:-${VERSION_CODENAME}}
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $arch
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker compose version
