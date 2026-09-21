"""App Home（Slack で Kei Agent を開いたときのタブ）に出す設定画面。

置くのは、テーマごとに許可した接続先と、決まった時刻の処理の時刻だけ（docs/design.md の9章）。
基本の接続先など `config.toml` の柵は出さない。
"""

from __future__ import annotations

import time

from kei_agent import settings
from kei_agent.config import Config
from kei_agent.slack_text import format_duration
from kei_agent.store import Store

ADD_DOMAIN_CALLBACK = "kei_agent_add_domain"
REFRESH_ACTION = "kei_agent_home_refresh"
# 決まった時刻の処理は、スレッドを持たない実行として記録される
TRIGGER_LABELS = {"message": "依頼", "job": "ジョブの結果", "domain": "接続先の返事", "voice": "声からの依頼",
                  "night": "夜間の Task", "handoff": "引き継ぎ", "literature": "先行研究の新着",
                  "daily": "Daily", "review": "振り返り"}


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    button = {"type": "button", "action_id": action_id, "value": value,
              "text": {"type": "plain_text", "text": text}}
    if style:
        button["style"] = style
    return button


def _now_working(store: Store, now: float | None = None) -> list[str]:
    """いま動いている依頼、走っているジョブ、返事待ちのスレッドを、短い行にして返す。"""
    now = time.time() if now is None else now
    lines = []
    for run in store.open_runs():
        kind = TRIGGER_LABELS.get(run["trigger"], run["trigger"])
        lines.append(f"⏳ *#{run['channel_name']}* {kind}（{format_duration(now - run['started_at'])}）")
    for job in store.active_jobs():
        started = "実行中" if job.status == "running" else "順番待ち"
        lines.append(f"🧪 ジョブ {job.id}「{job.name}」{started}"
                     f"（投入から {format_duration(now - job.submitted_at)}）")
    for row in store.threads_awaiting():
        lines.append(f"❓ *#{row['channel_name']}* 返事待ち（{format_duration(now - row['awaiting_since'])}）")
    return lines


def build_home(config: Config, store: Store, theme_names: list[str], is_owner: bool) -> dict:
    if not is_owner:
        return {"type": "home", "blocks": [_mrkdwn("Kei Agent の設定は、依頼者だけが変えられます。")]}

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Kei Agent の設定"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "ここで変えた内容は、再起動なしで次の作業から効きます。書き込み先や読ませない場所などは `config.toml` で管理します"}]},
        {"type": "divider"},
        _mrkdwn("*いま動いているもの*"),
    ]
    working = _now_working(store)
    blocks.append(_mrkdwn("\n".join(working)) if working else
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": "いまは何も動いていません"}]})
    blocks.append({"type": "actions", "elements": [_button("最新にする", REFRESH_ACTION, "refresh")]})

    blocks += [
        {"type": "divider"},
        _mrkdwn("*接続先*"),
        # Slack の mrkdwn は、閉じる * の直後に全角の文字が続くと太字にならないので、説明は別の行にする
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "テーマごとに許可した接続先です。チャンネルをアーカイブすると消えます"}]},
    ]
    domains = settings.all_theme_domains(store)
    for theme in theme_names:
        allowed = domains.get(theme, [])
        # テーマ名は先頭の番号を外したもの（themes.theme_name）。`#` を付けると
        # `#10_amr-query` というチャンネル名とずれて、別のものに見えてしまう
        blocks.append(_mrkdwn(f"*{theme}*" + ("" if allowed else "\n許可した接続先はまだありません")))
        for domain in allowed:
            blocks.append({
                "type": "section", "text": {"type": "mrkdwn", "text": f"`{domain}`"},
                "accessory": _button("外す", "kei_agent_home_remove_domain", f"{theme}\t{domain}", "danger"),
            })
    if theme_names:
        blocks.append({"type": "actions", "elements": [_button("接続先を足す", "kei_agent_home_add_domain", "add")]})
    else:
        blocks.append(_mrkdwn("研究テーマのチャンネルがまだありません"))

    blocks += [
        {"type": "divider"},
        _mrkdwn("*声*"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "*知らせる*: 作業が終わったときなどに、机の上で声に出します。"
            "Stack-chan がいればそちら、いなければ Mac のスピーカーで鳴らします\n"
            "*聞く*: マイクを開けて、「けい」と呼びかけたら答えます。"
            "**切っている間はマイクを閉じます**（講義中などに録られないように）"}]},
        {"type": "actions", "elements": [
            _button("知らせるのを止める" if settings.voice_enabled(store) else "知らせる",
                    "kei_agent_home_toggle_voice", "voice"),
            _button("聞くのを止める" if settings.listening_enabled(store) else "聞く",
                    "kei_agent_home_toggle_listen", "listen",
                    "danger" if settings.listening_enabled(store) else None),
        ]},
    ]

    blocks += [{"type": "divider"}, _mrkdwn("*決まった時刻の処理*")]
    for name in settings.SCHEDULE_NAMES:
        hhmm, enabled = settings.schedule_setting(config, store, name)
        timepicker = {"type": "timepicker", "action_id": f"kei_agent_home_time:{name}",
                      "placeholder": {"type": "plain_text", "text": "時刻"}}
        if hhmm:
            timepicker["initial_time"] = hhmm
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{settings.SCHEDULE_LABELS[name]}（{'動かす' if enabled else '止めている'}）"},
            "accessory": timepicker,
        })
        blocks.append({"type": "actions", "elements": [
            _button("止める" if enabled else "動かす", f"kei_agent_home_toggle:{name}", name),
        ]})
    return {"type": "home", "blocks": blocks}


def build_add_domain_modal(theme_names: list[str]) -> dict:
    options = [{"text": {"type": "plain_text", "text": f"#{t}"}, "value": t} for t in theme_names]
    return {
        "type": "modal",
        "callback_id": ADD_DOMAIN_CALLBACK,
        "title": {"type": "plain_text", "text": "接続先を足す"},
        "submit": {"type": "plain_text", "text": "足す"},
        "close": {"type": "plain_text", "text": "やめる"},
        "blocks": [
            {"type": "input", "block_id": "theme", "label": {"type": "plain_text", "text": "テーマ"},
             "element": {"type": "static_select", "action_id": "value", "options": options}},
            {"type": "input", "block_id": "domain", "label": {"type": "plain_text", "text": "ドメイン"},
             "hint": {"type": "plain_text", "text": "例: zenodo.org。*.example.com のように、まとめて許可することもできます"},
             "element": {"type": "plain_text_input", "action_id": "value"}},
        ],
    }


def read_add_domain(view: dict) -> tuple[str, str]:
    values = view.get("state", {}).get("values", {})
    theme = (values.get("theme", {}).get("value", {}).get("selected_option") or {}).get("value", "")
    domain = (values.get("domain", {}).get("value", {}).get("value") or "").strip().lower()
    return theme, domain
