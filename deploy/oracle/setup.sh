#!/usr/bin/env bash
# AInvestor — despliegue en Oracle Cloud VM (Ubuntu + Docker)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ainvestor}"
REPO_URL="${REPO_URL:-https://github.com/Alexfh94/AInvestor.git}"
BRANCH="${BRANCH:-master}"

echo "==> AInvestor setup en $APP_DIR"

if ! command -v docker >/dev/null; then
  echo "Instalando Docker..."
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-plugin git
  sudo systemctl enable docker
  sudo systemctl start docker
  sudo usermod -aG docker "$USER"
fi

sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR"
  git pull --ff-only
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "IMPORTANTE: edita $APP_DIR/.env con tus API keys antes de arrancar:"
  echo "  nano $APP_DIR/.env"
  echo "  (CURSOR_API_KEY, TRADING_MODE=paper, etc.)"
  exit 1
fi

mkdir -p data
docker compose up -d --build

PUBLIC_IP=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')
echo ""
echo "============================================"
echo " AInvestor desplegado"
echo " URL: http://${PUBLIC_IP}:8000"
echo " Health: http://${PUBLIC_IP}:8000/health"
echo "============================================"
