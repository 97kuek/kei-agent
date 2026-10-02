"""Slack App の manifest を作る（`kei-agent manifest`）。オンにしたモジュールのスラッシュコマンドを足す。

Slack の https://api.slack.com/apps → Create New App → From a manifest に貼る（作ったあとは App Manifest の画面で
貼り直す。権限が変わったら Install App で入れ直す）。リポジトリの slack/manifest.yaml は、組み込みのモジュールを
全部オンにしたときのもの（テストで、このコードの出力と同じか確かめる）。
"""

from __future__ import annotations

import argparse
import json

from kei_agent.configuration.config import Config, load_config
from kei_agent.framework import modules

HEADER = """# Slack App「Kei Agent」の manifest。kei-agent manifest が作る（組み込みのモジュールを全部オンにしたとき）。
# https://api.slack.com/apps → Create New App → From a manifest で貼り付ける。
# 自分の設定（オンにしたモジュール）に合わせたものは kei-agent manifest で出す。手で直さない
"""
# 本体が使う Slack の権限。スラッシュコマンドを持つモジュールがあれば commands を足す
SCOPES = ("app_mentions:read", "channels:history", "channels:read", "groups:history", "groups:read", "chat:write",
          "files:read", "files:write", "reactions:read", "reactions:write", "assistant:write")
EVENTS = ("app_mention", "message.channels", "message.groups", "member_joined_channel", "channel_rename",
          "group_rename", "reaction_added", "reaction_removed", "app_home_opened")


def build(config: Config, name: str = "Kei Agent") -> dict:
    """manifest の中身（Slack の書き方のまま）。"""
    commands = [
        {"command": f"/{command}", "description": description[:100],
         **({"usage_hint": spec.slash_hints[command]} if command in spec.slash_hints else {}),
         "should_escape": False}
        for spec in modules.enabled(config.modules) for command, description in spec.slash_commands.items()]
    features: dict = {
        "bot_user": {"display_name": name, "always_online": True},
        # AI アプリとして扱う。作業中のステータス（agents.sessions.setStatus）と、返事を流しながら見せる表示
        # （chat.startStream）に要る
        "agent_view": {"agent_description": "研究・授業・仕事の用事を受け取り、担当のエージェントが進めて結果を返す"},
    }
    if commands:
        features["slash_commands"] = commands
    features["app_home"] = {"home_tab_enabled": True, "messages_tab_enabled": True,
                            "messages_tab_read_only_enabled": False}
    return {
        "display_information": {
            "name": name,
            "description": "頼まれた用事を担当のエージェントに振り分けて進めるアシスタント。経過と結果をスレッドに返す",
            "background_color": "#2c3e50",
        },
        "features": features,
        "oauth_config": {"scopes": {"bot": [*SCOPES, *(["commands"] if commands else [])]}},
        "settings": {
            "event_subscriptions": {"bot_events": list(EVENTS)},
            # ボタン（引き継ぎの提案など）と、App Home の画面に使う
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def to_yaml(data: dict | list, indent: int = 0) -> str:
    """manifest の形（表・配列・文字・真偽）を YAML にする。文字はいつも二重引用符で囲む（JSON の書き方は YAML でも読める）。"""
    pad = "  " * indent
    lines: list[str] = []

    def scalar(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return json.dumps(value, ensure_ascii=False)

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {scalar(value)}")
    else:
        for item in data:
            if isinstance(item, dict):
                first, *rest = item.items()
                lines.append(f"{pad}- {first[0]}: {scalar(first[1])}")
                lines += [f"{pad}  {key}: {scalar(value)}" for key, value in rest]
            else:
                lines.append(f"{pad}- {scalar(item)}")
    return "\n".join(lines)


def render(config: Config, name: str = "Kei Agent") -> str:
    return HEADER + to_yaml(build(config, name)) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kei-agent manifest",
                                     description="オンにしたモジュールに合わせた Slack App の manifest を出す")
    parser.add_argument("--name", default="Kei Agent", help="Slack に出す App の名前")
    args = parser.parse_args(argv)
    print(render(load_config(), args.name), end="")
    return 0
