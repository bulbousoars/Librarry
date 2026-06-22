#!/usr/bin/env bash
# Install Librarry on mediaprod (or any Linux host with Python 3.11+).
set -euo pipefail

INSTALL_ROOT="${LIBRARRY_ROOT:-/mnt/storage/docker/scripts/librarry}"
REPO_SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"

echo "Installing Librarry to ${INSTALL_ROOT}"
sudo mkdir -p "${INSTALL_ROOT}"/{state,logs}
sudo chown -R "${USER}:${USER}" "${INSTALL_ROOT}"

python3 -m pip install --user -e "${REPO_SRC}"

if [[ ! -f "${INSTALL_ROOT}/config.yaml" ]]; then
  cp "${REPO_SRC}/config.example.yaml" "${INSTALL_ROOT}/config.yaml"
  echo "Created ${INSTALL_ROOT}/config.yaml — edit paths and run secrets setup."
fi

"${HOME}/.local/bin/librarry" init --config "${INSTALL_ROOT}/config.yaml" || librarry init --config "${INSTALL_ROOT}/config.yaml"

cat <<EOF

Installed. Next steps:

  librarry secrets init --config ${INSTALL_ROOT}/config.yaml
  librarry secrets set hardcover_token --config ${INSTALL_ROOT}/config.yaml
  # ... set other secrets (see README)

  librarry check --config ${INSTALL_ROOT}/config.yaml
  librarry run --config ${INSTALL_ROOT}/config.yaml

Cron example:
  */30 * * * * librarry run --config ${INSTALL_ROOT}/config.yaml >> ${INSTALL_ROOT}/logs/cron.log 2>&1
EOF
