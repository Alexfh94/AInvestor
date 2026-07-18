#!/usr/bin/env bash
# AInvestor — despliegue en GCP e2-micro (Debian/Ubuntu + Docker)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ainvestor}"
REPO_URL="${REPO_URL:-https://github.com/Alexfh94/AInvestor.git}"
BRANCH="${BRANCH:-master}"

echo "==> AInvestor setup en $APP_DIR"

if ! command -v docker >/dev/null; then
  echo "Instalando Docker..."
  sudo apt-get update
  sudo apt-get install -y docker.io git curl
  sudo systemctl enable docker
  sudo systemctl start docker
  sudo usermod -aG docker "$USER"
fi

sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git pull --ff-only
elif [ ! -f "$APP_DIR/docker-compose.yml" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "IMPORTANTE: edita $APP_DIR/.env con tus API keys antes de arrancar:"
  echo "  nano $APP_DIR/.env"
  exit 1
fi

mkdir -p data

# e2-micro tiene ~1GB RAM; swap evita cuelgues por presión de memoria
if [ ! -f /swapfile ]; then
  echo "==> Configurando swap 1GB..."
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if docker compose version >/dev/null 2>&1; then
  sudo docker compose up -d --build
elif command -v docker-compose >/dev/null; then
  sudo docker-compose up -d --build
else
  echo "ERROR: docker compose no disponible"
  exit 1
fi

PUBLIC_IP=$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || curl -s ifconfig.me)
echo ""
echo "============================================"
echo " AInvestor desplegado (GCP)"
echo " URL: http://${PUBLIC_IP}:8000"
echo " Health: http://${PUBLIC_IP}:8000/health"
echo "============================================"
