#!/usr/bin/env bash
# Deploy no VPS: git pull, Next.js (PM2 trading-ui), Python bot + macro radar (PM2).
# Executar na raiz do repositório clonado no servidor (ex.: ~/algo-trading).
set -euo pipefail

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

ok()   { echo -e "${GREEN}${BOLD}[OK]${NC} ${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}${BOLD}[WARN]${NC} ${YELLOW}$*${NC}"; }
err()  { echo -e "${RED}${BOLD}[ERR]${NC} ${RED}$*${NC}" >&2; }
info() { echo -e "${CYAN}${BOLD}[..]${NC} $*"; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${PROJECT_ROOT}/frontend"
VENV_PY="${PROJECT_ROOT}/venv/bin/python"
VENV_PIP="${PROJECT_ROOT}/venv/bin/pip"

cd "${PROJECT_ROOT}"

info "Repositório: ${PROJECT_ROOT}"

# --- Git ---
info "A atualizar código (git pull origin main)…"
if ! git pull origin main; then
  err "git pull falhou. Verifica rede, credenciais e branch."
  exit 1
fi
ok "Código atualizado a partir de origin/main."

# --- Front-end ---
if [[ ! -d "${FRONTEND_DIR}" ]]; then
  err "Pasta frontend não encontrada: ${FRONTEND_DIR}"
  exit 1
fi

info "Front-end: npm install…"
cd "${FRONTEND_DIR}"
npm install

info "Front-end: npm run build…"
npm run build

info "Front-end: PM2 (trading-ui)…"
if pm2 restart trading-ui 2>/dev/null; then
  ok "Front-end: processo trading-ui reiniciado."
else
  pm2 start npm --name trading-ui --cwd "${FRONTEND_DIR}" -- start
  ok "Front-end: trading-ui iniciado com PM2."
fi

# --- Python / back-end ---
cd "${PROJECT_ROOT}"

if [[ ! -f "${PROJECT_ROOT}/venv/bin/activate" ]]; then
  err "Ambiente virtual não encontrado. Cria com: python3 -m venv venv"
  exit 1
fi

# shellcheck source=/dev/null
source "${PROJECT_ROOT}/venv/bin/activate"

info "Algo-trading: pip install -r requirements.txt…"
if [[ -x "${VENV_PIP}" ]]; then
  "${VENV_PIP}" install -r "${PROJECT_ROOT}/requirements.txt"
else
  python -m pip install -r "${PROJECT_ROOT}/requirements.txt"
fi
ok "Dependências Python instaladas."

info "PM2: trading-bot (main.py)…"
if [[ ! -f "${PROJECT_ROOT}/main.py" ]]; then
  err "main.py não encontrado em ${PROJECT_ROOT}"
  exit 1
fi
if pm2 restart trading-bot 2>/dev/null; then
  ok "trading-bot reiniciado."
else
  pm2 start "${PROJECT_ROOT}/main.py" --name trading-bot --interpreter "${VENV_PY}" --cwd "${PROJECT_ROOT}"
  ok "trading-bot iniciado."
fi

if [[ -f "${PROJECT_ROOT}/macro_radar.py" ]]; then
  info "PM2: macro-radar (macro_radar.py)…"
  if pm2 restart macro-radar 2>/dev/null; then
    ok "macro-radar reiniciado."
  else
    pm2 start "${PROJECT_ROOT}/macro_radar.py" --name macro-radar --interpreter "${VENV_PY}" --cwd "${PROJECT_ROOT}"
    ok "macro-radar iniciado."
  fi
else
  warn "macro_radar.py ausente — macro-radar não foi alterado. Adiciona o script para o PM2 o gerir."
fi

echo ""
echo -e "${BLUE}${BOLD}=== Estado PM2 ===${NC}"
pm2 list

ok "Deploy concluído."
