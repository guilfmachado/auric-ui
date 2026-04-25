#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
VPS_HOST="185.182.8.127"
VPS_USER="root"
REMOTE_DIR="/var/www/html/frontend"

echo "==> Building frontend (Next static export)..."
cd "${FRONTEND_DIR}"
npm run build

echo "==> Preparing remote directory ${VPS_USER}@${VPS_HOST}:${REMOTE_DIR} ..."
ssh "${VPS_USER}@${VPS_HOST}" "mkdir -p '${REMOTE_DIR}'"

echo "==> Uploading frontend/out to VPS via scp..."
scp -r "${FRONTEND_DIR}/out/." "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

echo "==> Restarting nginx on VPS..."
ssh "${VPS_USER}@${VPS_HOST}" "systemctl restart nginx"

echo "✅ Deploy concluido com sucesso."
