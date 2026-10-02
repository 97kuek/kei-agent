#!/bin/zsh
# 手の口（MCP）を、OpenAI の Secure MCP Tunnel で ChatGPT（Dots）に届ける。launchd から起動する（deploy/install.sh tunnel）。
# トンネルのプログラム（tunnel-client）は、この Mac から OpenAI へ出ていくだけで、Mac の口は外に開かない。
# 住所とトンネルの番号は config.toml の [hands] の url と tunnel。鍵（CONTROL_PLANE_API_KEY）はトンネルだけのファイル
# kei-agent-tunnel.zsh に置く（ほかのプロセスに見せない）。手の口の合言葉は、トンネルが呼び出しに付けて渡す。
# トンネルのプログラムには、要るものだけを渡す（ほかの鍵は見せない）
set -eu

REPO="${0:A:h:h}"
source "$REPO/deploy/_common.sh"

require_secrets
source "$SECRETS"
TUNNEL_SECRETS="$SECRETS_DIR/kei-agent-tunnel.zsh"
if [[ ! -r "$TUNNEL_SECRETS" ]]; then
  echo "トンネルの鍵のファイルがありません: $TUNNEL_SECRETS（deploy/README.md の「手の口（MCP）」を参照）" >&2
  exit 1
fi
source "$TUNNEL_SECRETS"
drop_notion_secrets
: "${CONTROL_PLANE_API_KEY:?kei-agent-tunnel.zsh に CONTROL_PLANE_API_KEY を書いてください}"
: "${KEI_AGENT_HANDS_TOKEN:?kei-agent.zsh に KEI_AGENT_HANDS_TOKEN を書いてください}"

trim_launchd_log tunnel-launchd.log
cd "$REPO"
hands=(${(f)"$("$REPO/.venv/bin/python" -m kei_agent.configuration.paths hands)"})
URL="${hands[1]:-}" TUNNEL="${hands[2]:-}"
if [[ -z "$URL" || -z "$TUNNEL" ]]; then
  echo "config.toml の [hands] に url と tunnel を書いてください" >&2
  exit 1
fi
if ! command -v tunnel-client >/dev/null; then
  echo "tunnel-client がありません（brew install openai/tools/tunnel-client）" >&2
  exit 1
fi

# 様子を見る口（/readyz）は、doctor が見る（src/kei_agent/operations/doctor.py の TUNNEL_READY）
exec env -i HOME="$HOME" PATH="$PATH" \
  CONTROL_PLANE_API_KEY="$CONTROL_PLANE_API_KEY" \
  KEI_AGENT_HANDS_BEARER="Bearer $KEI_AGENT_HANDS_TOKEN" \
  tunnel-client run \
    --control-plane.tunnel-id "$TUNNEL" \
    --control-plane.api-key env:CONTROL_PLANE_API_KEY \
    --mcp.server-url "url=$URL/mcp,channel=main" \
    --mcp.extra-headers 'Authorization: env:KEI_AGENT_HANDS_BEARER' \
    --mcp.discovery-extra-headers 'Authorization: env:KEI_AGENT_HANDS_BEARER' \
    --health.listen-addr 127.0.0.1:8784 \
    --log.format json
