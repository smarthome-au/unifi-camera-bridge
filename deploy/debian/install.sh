#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  echo 'Usage: sudo ./install.sh [--check] [--bind-address LAN_IP] [--admin-port PORT]'
  echo 'Installs Unifi Camera Bridge on Debian 12/13 (amd64 or arm64).'
  exit 0
fi
[[ $(uname -s) == Linux && -f /etc/debian_version ]] || { echo 'Run this installer on Debian Linux.' >&2; exit 1; }
[[ $EUID == 0 ]] || { echo 'Run with sudo: sudo ./install.sh' >&2; exit 1; }
# Verify the shipped payload before installing prerequisites or running Python.
sha256sum --check --strict SHA256SUMS.txt >/dev/null
if ! command -v python3 >/dev/null || ! command -v ip >/dev/null || [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
  for arg in "$@"; do
    [[ $arg != --check ]] || { echo 'Read-only check needs python3, iproute2 and ca-certificates installed.' >&2; exit 1; }
  done
  # Guard Debian version before making any package changes.
  . /etc/os-release
  [[ $ID == debian && ( $VERSION_ID == 12 || $VERSION_ID == 13 ) ]] || { echo 'Supported hosts: Debian 12 or 13.' >&2; exit 1; }
  case $(dpkg --print-architecture) in amd64|arm64) ;; *) echo 'Supported CPUs: amd64 and arm64.' >&2; exit 1;; esac
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove python3 iproute2 ca-certificates
fi
exec python3 ./install.py "$@"
