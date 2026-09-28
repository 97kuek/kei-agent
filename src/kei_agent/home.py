"""App Home（Slack で Kei Agent を開いたときのタブ）に出す設定画面。

見出しと操作だけの1画面にする（説明文は置かない。2026-09-26）。置くのは、動いているもの、担当ごとの AI、
定期実行の時刻とオン・オフ、モジュールの項目（class Module の home。声の「知らせる」「聞く」など）、
テーマごとに許可した接続先だけ。
基本の接続先など `config.toml` の柵は出さない。
"""

from __future__ import annotations

import time
from datetime import datetime

from kei_agent import modules, settings
from kei_agent.config import Config
from kei_agent.slack_text import format_duration
from kei_agent.store import Store

ADD_DOMAIN_CALLBACK = "kei_agent_add_domain"
REFRESH_ACTION = "kei_agent_home_refresh"
PROVIDER_ACTION = "kei_agent_home_provider"      # :<担当>
TIME_ACTION = "kei_agent_home_time"              # :<定期実行>
SCHEDULES_ACTION = "kei_agent_home_schedules"
MODULE_ACTION = "kei_agent_home_module"          # :<モジュール>:<名前>
REMOVE_DOMAIN_ACTION = "kei_agent_home_remove_domain"
ADD_DOMAIN_ACTION = "kei_agent_home_add_domain"
# 本体の実行役の表示名。モジュールの実行役は module.toml の label（agent_labels）
CORE_AGENT_LABELS: dict[str, str] = {}
CROSS_AGENT_LABELS = {"router": "振り分け"}
# 決まった時刻の処理は、スレッドを持たない実行として記録される
TRIGGER_LABELS = {"message": "依頼", "job": "ジョブの結果", "domain": "接続先の返事", "voice": "声からの依頼",
                  "night": "夜間の Task", "handoff": "引き継ぎ", "daily": "Daily", "review": "振り返り"}


def agent_labels(config: Config) -> dict[str, str]:
    """App Home で AI を選ぶ実行役と表示名（本体の担当、使うモジュール、横断の係の順）。"""
    labels = dict(CORE_AGENT_LABELS)
    labels.update({spec.name: spec.label for spec in modules.enabled(config.modules) if spec.actor})
    labels.update(CROSS_AGENT_LABELS)
    return labels


def _mrkdwn(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _option(text: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": text}, "value": value}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    button = {"type": "button", "action_id": action_id, "value": value,
              "text": {"type": "plain_text", "text": text}}
    if style:
        button["style"] = style
    return button


def module_action_id(module: str, name: str) -> str:
    """モジュールの項目の action_id（押されると、そのモジュールの on_home_action(name, action)）。"""
    return f"{MODULE_ACTION}:{module}:{name}"


def checkboxes(action_id: str, options: dict[str, str], chosen: set[str]) -> dict:
    """値 → 表示名のチェック。Slack は空の initial_options を受け付けないので、選んだものが無ければ付けない。"""
    element = {"type": "checkboxes", "action_id": action_id,
               "options": [_option(text, value) for value, text in options.items()]}
    initial = [_option(text, value) for value, text in options.items() if value in chosen]
    if initial:
        element["initial_options"] = initial
    return element


def _now_working(config: Config, store: Store, now: float | None = None) -> list[str]:
    """いま動いている依頼、走っているジョブ、上限が明けるのを待っている依頼、返事待ちのスレッドを、短い行にして返す。"""
    now = time.time() if now is None else now
    lines = []
    for run in store.open_runs():
        kind = TRIGGER_LABELS.get(run["trigger"]) or settings.schedule_label(config, run["trigger"])
        lines.append(f"⏳ *#{run['channel_name']}* {kind}（{format_duration(now - run['started_at'])}）")
    for job in store.active_jobs():
        started = "実行中" if job.status == "running" else "順番待ち"
        lines.append(f"🧪 ジョブ {job.id}「{job.name}」{started}"
                     f"（投入から {format_duration(now - job.submitted_at)}）")
    for _, payload in store.pending_deferred("request"):
        until = store.limit_until(str(payload.get("provider") or ""))
        when = f"{datetime.fromtimestamp(until):%H:%M} ごろから" if until > now else "まもなく"
        lines.append(f"⏸ *#{payload['channel_name']}* 上限が明けるのを待っている（{when}）")
    for row in store.threads_awaiting():
        lines.append(f"❓ *#{row['channel_name']}* 返事待ち（{format_duration(now - row['awaiting_since'])}）")
    return lines


def build_home(config: Config, store: Store, theme_names: list[str], is_owner: bool,
               module_sections: list[tuple[str, list[dict]]] = ()) -> dict:
    """module_sections は、モジュールの表示名と項目（class Module の home が返した blocks）。定期実行の下に並べる。"""
    if not is_owner:
        return {"type": "home", "blocks": [_mrkdwn("設定を変えられるのは依頼者だけです")]}

    working = _now_working(config, store)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Kei Agent"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*動いているもの*"},
         "accessory": _button("更新", REFRESH_ACTION, "refresh")},
        _mrkdwn("\n".join(working)) if working else _context("なし"),
        {"type": "divider"},
        _mrkdwn("*AI*"),
    ]
    for agent, label in agent_labels(config).items():
        provider = settings.agent_profile(config, store, agent).provider
        select = {"type": "static_select", "action_id": f"{PROVIDER_ACTION}:{agent}",
                  "placeholder": {"type": "plain_text", "text": "未選択"},
                  "options": [_option("Claude", "claude"), _option("Codex", "codex")]}
        if provider:
            select["initial_option"] = _option(provider.title(), provider)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": label}, "accessory": select})

    names = settings.schedule_names(config)
    running = {name for name in names if settings.schedule_setting(config, store, name)[1]}
    blocks += [
        {"type": "divider"},
        _mrkdwn("*定期実行*"),
        {"type": "actions", "elements": [
            checkboxes(SCHEDULES_ACTION, {name: settings.schedule_label(config, name, short=True) for name in names},
                        running)]},
    ]
    for name in names:
        hhmm, _enabled = settings.schedule_setting(config, store, name)
        picker = {"type": "timepicker", "action_id": f"{TIME_ACTION}:{name}",
                  "placeholder": {"type": "plain_text", "text": "時刻"}}
        if hhmm:
            picker["initial_time"] = hhmm
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": settings.schedule_label(config, name)},
                       "accessory": picker})

    for label, items in module_sections:
        blocks += [{"type": "divider"}, _mrkdwn(f"*{label}*"), *items]

    blocks += [{"type": "divider"}, _mrkdwn("*接続先*")]
    domains = settings.all_theme_domains(store)
    for theme in theme_names:
        allowed = domains.get(theme, [])
        # テーマ名は先頭の番号を外したもの（themes.theme_name）。`#` を付けると
        # `#10_amr-query` というチャンネル名とずれて、別のものに見えてしまう
        block = _mrkdwn(f"*{theme}*  " + ("、".join(f"`{domain}`" for domain in allowed) if allowed else "なし"))
        if allowed:
            block["accessory"] = {"type": "static_select", "action_id": REMOVE_DOMAIN_ACTION,
                                  "placeholder": {"type": "plain_text", "text": "外す"},
                                  "options": [_option(domain, f"{theme}\t{domain}") for domain in allowed]}
        blocks.append(block)
    blocks.append({"type": "actions", "elements": [_button("足す", ADD_DOMAIN_ACTION, "add")]} if theme_names
                  else _context("研究テーマのチャンネルはまだありません"))
    return {"type": "home", "blocks": blocks}


def build_add_domain_modal(theme_names: list[str]) -> dict:
    options = [_option(theme, theme) for theme in theme_names]
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
             "hint": {"type": "plain_text", "text": "例: zenodo.org、*.example.com"},
             "element": {"type": "plain_text_input", "action_id": "value"}},
        ],
    }


def read_add_domain(view: dict) -> tuple[str, str]:
    values = view.get("state", {}).get("values", {})
    theme = (values.get("theme", {}).get("value", {}).get("selected_option") or {}).get("value", "")
    domain = (values.get("domain", {}).get("value", {}).get("value") or "").strip().lower()
    return theme, domain
