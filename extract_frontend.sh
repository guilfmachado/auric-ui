#!/usr/bin/env bash
# Extrai frontend/ para um novo repositório Git irmão: ../auric-ui
# Uso: na raiz do algo-trading, executar ./extract_frontend.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$REPO_ROOT")"
TARGET="${PARENT}/auric-ui"
SOURCE="${REPO_ROOT}/frontend"

if [[ ! -d "$SOURCE" ]]; then
  echo "Erro: pasta frontend não encontrada em: $SOURCE" >&2
  exit 1
fi

if [[ -e "$TARGET" ]]; then
  echo "Erro: já existe um caminho em: $TARGET" >&2
  echo "      Remove ou renomeia antes de voltar a correr o script." >&2
  exit 1
fi

echo "Origem:  $SOURCE"
echo "Destino: $TARGET"
mkdir -p "$TARGET"

# Copia tudo (inclui ficheiros ocultos: .env.local, .gitignore, etc.)
# Usa rsync para cópia recursiva fiável; fallback para cp -a se rsync não existir.
if command -v rsync >/dev/null 2>&1; then
  rsync -a "${SOURCE}/" "${TARGET}/"
else
  # macOS / Linux: copia conteúdo de frontend/ incluindo dotfiles
  (cd "$SOURCE" && cp -a . "${TARGET}/")
fi

cd "$TARGET"

if [[ ! -d .git ]]; then
  git init
  git add -A
  if [[ -z "$(git status --porcelain)" ]]; then
    echo "Aviso: nada por fazer commit (staging vazio — verifica .gitignore)." >&2
  else
    git commit -m "Initial commit: Auric UI (extracted from algo-trading frontend)"
  fi
else
  echo "Aviso: já existe .git em $TARGET — não foi feito git init." >&2
fi

echo ""
echo "Concluído. Repositório local em: $TARGET"
echo ""
echo "Próximos passos:"
echo "  1. No GitHub, cria um repositório vazio (ex.: auric-ui)."
echo "  2. cd \"$TARGET\""
echo "  3. git remote add origin https://github.com/<USER>/<REPO>.git"
echo "  4. git branch -M main   # se necessário"
echo "  5. git push -u origin main"
echo ""
