#!/bin/zsh
# ~/research を非公開の GitHub リポジトリにして、毎晩のバックアップ（config.toml の [maintenance]）を使えるようにする。
# 1回だけ実行する。何度実行しても壊れない。
# 使い方: deploy/backup-init.sh [リポジトリ名（既定: research-data）]
set -eu

REPO="${0:A:h:h}"
NAME="${1:-research-data}"
ROOT="$HOME/research"
OWNER="$(gh api user --jq .login)"

mkdir -p "$ROOT"
cd "$ROOT"
[[ -d .git ]] || git init -q -b main

if [[ ! -f .gitignore ]]; then
  cat > .gitignore <<'IGNORE'
# Ezra が作る一時的なもの
.ezra/requests/
.claude/
__pycache__/
.venv/
*.tmp
.DS_Store
IGNORE
fi

# launchd から push できるよう、このリポジトリだけ gh の認証を使う
git config --local --replace-all credential.https://github.com.helper ''
git config --local --add credential.https://github.com.helper '!gh auth git-credential'

if ! gh repo view "$OWNER/$NAME" >/dev/null 2>&1; then
  gh repo create "$OWNER/$NAME" --private --description "Ezra の研究データのバックアップ（~/research）"
fi
git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$NAME.git"

# Ezra の状態を書き出してから、最初のコミットを作る
(cd "$REPO" && uv run --frozen python -c 'from ezra.config import load_config; from ezra.maintenance import dump_state, exclude_large_files; c = load_config(); dump_state(c); exclude_large_files(c.research_root)')
git add -A
git diff --cached --quiet || git commit -q -m "研究データの保存を始める"
git push -q -u origin main
echo "バックアップ先: https://github.com/$OWNER/$NAME （非公開）"
