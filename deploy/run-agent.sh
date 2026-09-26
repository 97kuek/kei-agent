#!/bin/zsh
# launchd から研究・大学・仕事のエージェント（A2A サーバー）を起動する。127.0.0.1 でだけ待ち受ける。
# 使い方: deploy/run-agent.sh <research|course|work|knowledge>。どれも同じ形で、違うのは名前だけ。
set -eu

AGENT="${1:-}"
case "$AGENT" in
  research|course|work|knowledge) ;;
  *)
    echo "知らないエージェントです: ${AGENT:-（名前なし）}（research / course / work のどれか）" >&2
    exit 1
    ;;
esac

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# 秘密情報は、共通のものと、このエージェントだけのものに分けてある（docs/architecture.md の「振り分けと A2A」）。
# こうすると、ほかのエージェントのトークンがこのプロセスに載らない。
# エージェントごとのファイルはどのエージェントでも任意で、共通のもののあとに読む（同じ名前は上書きできる）
SECRETS="$HOME/.config/zsh/local/kei-agent.zsh"
AGENT_SECRETS="$HOME/.config/zsh/local/kei-agent-$AGENT.zsh"
if [[ ! -r "$SECRETS" ]]; then
  echo "秘密情報のファイルがありません: $SECRETS（deploy/README.md を参照）" >&2
  exit 1
fi
source "$SECRETS"
[[ -r "$AGENT_SECRETS" ]] && source "$AGENT_SECRETS"

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"
drop_notion_secrets

# ログは launchd の標準出力（~/Library/Logs/kei-agent/<名前>-launchd.log）に出る
trim_launchd_log "$AGENT-launchd.log"

cd "$REPO"
exec uv run --frozen --group "$AGENT" "kei-agent-$AGENT"
