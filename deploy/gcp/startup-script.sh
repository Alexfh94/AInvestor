#!/usr/bin/env bash
# GCP VM first-boot: Docker + firewall + base packages
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose git curl ufw

systemctl enable docker
systemctl start docker

USER_NAME="${SUDO_USER:-$(logname 2>/dev/null || echo ubuntu)}"
if id "$USER_NAME" &>/dev/null; then
  usermod -aG docker "$USER_NAME" || true
fi

ufw allow OpenSSH
ufw allow 8000/tcp
ufw --force enable

mkdir -p /opt/ainvestor/data
chown -R "$USER_NAME:$USER_NAME" /opt/ainvestor 2>/dev/null || true

echo "AInvestor GCP startup complete" > /var/log/ainvestor-startup.log
