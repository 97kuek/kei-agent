"""App Home（Slack で Kei Agent を開いたときのタブ）に出す設定画面。

置くのは、テーマごとに許可した接続先と、決まった時刻の処理の時刻だけ（docs/plan.md の11章）。
基本の接続先など `config.toml` の柵は出さない。
"""

from __future__ import annotations

from kei_agent import settings
from kei_agent.config import Config
from kei_agent.store import Store

ADD_DOMAIN_CALLBACK = "kei_agent_add_domain"


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    button = {"type": "button", "action_id": action_id, "value": value,
              "text": {"type": "plain_text", "text": text}}
    if style:
        button["style"] = style
    return button


def build_home(config: Config, store: Store, theme_names: list[str], is_owner: bool) -> dict:
    if not is_owner:
        return {"type": "home", "blocks": [_mrkdwn("Kei Agent の設定は、依頼者だけが変えられます。")]}

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Kei Agent の設定"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "ここで変えた内容は、再起動なしで次の作業から効きます。書き込み先や読ませない場所などは `config.toml` で管理します"}]},
        {"type": "divider"},
        _mrkdwn("*接続先*"),
        # Slack の mrkdwn は、閉じる * の直後に全角の文字が続くと太字にならないので、説明は別の行にする
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "テーマごとに許可した接続先です。チャンネルをアーカイブすると消えます"}]},
    ]
    domains = settings.all_theme_domains(store)
    for theme in theme_names:
        allowed = domains.get(theme, [])
        blocks.append(_mrkdwn(f"*#{theme}*" + ("" if allowed else "\n許可した接続先はまだありません")))
        for domain in allowed:
            blocks.append({
                "type": "section", "text": {"type": "mrkdwn", "text": f"`{domain}`"},
                "accessory": _button("外す", "kei_agent_home_remove_domain", f"{theme}\t{domain}", "danger"),
            })
    if theme_names:
        blocks.append({"type": "actions", "elements": [_button("接続先を足す", "kei_agent_home_add_domain", "add")]})
    else:
        blocks.append(_mrkdwn("研究テーマのチャンネルがまだありません"))

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
