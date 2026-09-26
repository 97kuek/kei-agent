#!/bin/zsh
# 取り込んだあとに、Kei Agent のプロセスを全部、新しい版で起動し直す（担当が古い版のまま残らないように）。
# 使い方: deploy/restart-all.sh
# 登録してあるもの（~/Library/LaunchAgents/com.kei-agent.*.plist）だけを、ゲートウェイ → 担当 → 本体の順に。
# 本体は起動したときに担当の版を確かめるので、最後にする。plist を変えたときは deploy/install.sh を使う
set -eu

DOMAIN="gui/$(id -u)"
labels=()
for plist in "$HOME"/Library/LaunchAgents/com.kei-agent.*.plist(N); do
  labels+=("${${plist:t}%.plist}")
done
if (( ${#labels} == 0 )); then
  echo "登録してある Kei Agent のプロセスがありません（deploy/install.sh を参照）" >&2
  exit 1
fi

ordered=(${(M)labels:#com.kei-agent.notion-gateway} ${labels:#com.kei-agent.(notion-gateway|assistant)} ${(M)labels:#com.kei-agent.assistant})
failed=0
for label in $ordered; do
  if launchctl kickstart -k "$DOMAIN/$label"; then
    echo "起動し直しました: $label"
  else
    echo "起動し直せません: $label" >&2
    failed=1
  fi
done
exit $failed
