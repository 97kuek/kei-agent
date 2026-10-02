# モジュールの作り方

- Kei Agent の機能は、どれもモジュール（`modules/<名前>/`）
- 自分のモジュールを足せば、コアを直さずに担当や定期処理を増やせる
- 組み込みのモジュール（リポジトリの `modules/`）が、そのまま見本になる
- 設計の決めごとは [`extensibility.md`](extensibility.md)、仕組みは [`architecture.md`](architecture.md)

## はじめる

```zsh
uv run kei-agent module new weather --ai --label 天気   # ひな形を作る（担当プロセスも持つなら --process）
uv run kei-agent module test weather                    # テスト（--process なら uv run --group agents …）
uv run kei-agent module add weather                     # オンにする。そのあとにやることを並べる
uv run kei-agent doctor                                 # 確かめる
```

1. ひな形は `~/.config/kei-agent/modules/weather/` にできる。設定はまだ変えない
2. `module.toml` の `description`、`module.py` の答え方、`weather.md`（指示書）を書く
3. テストを通す。本物の Slack・AI・秘密情報には触れない
4. `module add` でオンにし、起動し直す・チャンネルを作って招く・AI を選ぶ（`agents.csv` の `engine`）
5. Kei Agent に足して公開するなら `--builtin` でリポジトリの `modules/` に作り、プルリクエストにする（[CONTRIBUTING.md](../CONTRIBUTING.md)）

## フォルダの中身

| ファイル | 役目 | 要るとき |
|---|---|---|
| `module.toml` | 定義（名前・チャンネル・定期処理・AI・担当プロセス・設定・秘密情報） | いつも |
| `module.py` | 本体側の動き（`class Module` の差し込み口） | チャンネル・定期処理・コマンドなどを持つとき |
| `<名前>.md` | AI の指示書（`[actor] prompt`） | `[actor]` があるとき |
| `agent.py` | 担当プロセス（`SKILLS` と `class Executor`） | `[process]` があるとき |
| `service.py` | A2A ではない常駐のプロセス（`serve(config, port)`） | `[process] kind = "service"` のとき |
| `commands.py` | 手で動かすコマンド（`COMMANDS`） | 任意 |
| `plugin/` | skill と、安全のフック（Claude Code の plugin） | `[actor] plugin = true` のとき |
| `AGENTS.template.md` | 作業場に最初に置く前提のメモ `AGENTS.md`（`CLAUDE.md` はそれを読み込む1行） | 任意 |
| `tests/` | テスト（`kei-agent module test <名前>`） | 任意（ひな形に入っている） |

## 決まりごと

- 読み込んでよい Kei Agent の部品は窓口だけ: `module.py` は `kei_agent.api`、ほかのファイルは `kei_agent_a2a.api`
- 同じフォルダのファイルは `from . import texts` のように読める
- 枠の版は `api = 1`。合わない版のモジュールは読み込まない
- できること（`[actor]` の `files`・`shell`・`web`・`notion`・連携の道具）を超えることは、コアが実行のときに止める
- AI に書かせてよいのは、自分のフォルダ（`core.state_dir`）か、作業場だけ
- 秘密情報は `[secrets]` に名前と説明だけを書く。値はコード・ログ・Slack に出さない
- 指示書の返答は `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間だけが Slack に出る。印が無いと答えが空になる
- 名前は英小文字・数字・-（フォルダの名前と同じ）
- 用途・定期処理・チャンネルの種類・番地は、ほかのモジュールとぶつけない（ぶつかれば設定を読むときに断る）

## module.toml

- 見本は `modules/knowledge/module.toml`
- 知らないキーや形の違いは、読むときに断る（`src/kei_agent/framework/modules.py`）

```toml
api = 1                         # 枠の版
name = "knowledge"              # フォルダの名前と同じ
label = "知識"                  # App Home などに出す名前
description = "…"
# core_channels = ["improve"]   # 会話を受け持つ本体のチャンネル（Kei Agent のチャンネル）
# core_schedules = ["daily", "review"]   # 受け持つ本体の定期処理

[depends]
requires = []                   # 必須のモジュール
optional = ["notion"]           # あれば使うモジュール

[actor]                         # AI の実行役（provider は agents.csv の engine）
prompt = "knowledge.md"         # 指示書
# data = "company"              # 会社のデータを読む実行役（外へ出られない）。書かなければ own
notion = "none"                 # none / read / write
timeout_minutes = 10
default_use_case = "knowledge_answer"
# classify = "…"                # 用途を軽いモデルで選び分けるときの見分け方
# plugin = true                 # plugin/ の skill とフックを渡す
# workspace = "~/course"        # 作業場（無ければ状態の置き場の agents/<名前>）

# [[actor.connectors]]          # アカウントの連携。書いた道具だけを使える
# name = "outlook"
# claude_server = "claude_ai_Microsoft_365"
# claude_tools = ["outlook_calendar_search"]
# [[actor.connectors.codex_apps]]
# name = "Microsoft Outlook Calendar"
# namespace = "microsoft_outlook_calendar"
# tools = ["search_events"]

[use_cases.knowledge_pick]      # 用途ごとのモデル（コアの一覧の中からだけ）
claude = { model = "claude-haiku-4-5" }
codex = { model = "gpt-6-luna", effort = "low" }
# manual = true                 # [[knowledge-pick]] と書いたときだけ使う

[process]                       # 常駐のプロセス（持つなら）
port = 8792                     # 127.0.0.1 のこの番地
# kind = "service"              # A2A ではない口（service.py）

[channels]                      # チャンネルの種類 = 既定の名前（番号は外す）
knowledge = ["knowledge"]
# theme = ["*"]                 # ほかのどれでもないチャンネル（研究テーマ）
# project = ["work-*"]          # 頭が一致するチャンネル（プロジェクト）。作業場は担当のフォルダの下の <頭を除いた名前>

[schedules.reading]             # 定期処理
label = "読みもの"
default = "07:00"               # 空文字なら、既定では動かさない

# [slash_commands]              # スラッシュコマンド（kei-agent manifest が Slack App に載せる）
# stamp = "スタンプを押す"
# toggl = { description = "時間記録を始める・止める", usage_hint = "[start|stop]" }

# [settings]                    # 設定の既定（config.toml の [<名前>] で変えられる）
# place = "東京"

# [secrets]                     # 要る秘密情報（setup が聞き、doctor が確かめる）
# WEATHER_API_KEY = { description = "天気の API のキー", required = true, own_file = true }
```

| キー | 気をつけること |
|---|---|
| `[settings]` | 利用者が書けるのは、ここにある名前だけ。形が既定と違えば断る。オフにしても設定は残せる |
| `[secrets]` | `required`: 無いと動かない。`own_file`: `kei-agent-<名前>.zsh` に置く。`generate`: setup が値を作る。`group`: そろって初めて使う |
| `[channels]` ほか | `[channels]`・`core_channels`・`core_schedules`・`[schedules]` を書いたら `module.py` が要る |
| `[channels]` の種類 | `agents.csv` の行の `channels` で名前を変えられるのは、種類が1つのときと、ふつうの種類と頭が一致する種類（`"work-*"`）が1つずつのとき（仕事。書き方で分ける）。頭が一致する種類を持つ担当の作業場は状態の置き場の `agents/<名前>` で、`folder` はプロジェクトを並べる場所 |
| `[process]` | `agent.py`（`kind = "service"` なら `service.py`）が要る。起動し直すときは service が先 |
| 定期処理の順 | 同じ時刻なら、夜間の Task → モジュール（設定の `modules` の順）→ Daily → 振り返り → 保守 |

## module.py

- 見本は `modules/knowledge/module.py`
- 本体が起動するときに読み込み、書いた差し込み口だけを呼ぶ

```python
from kei_agent.api import Core, Request


class Module:
    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:                    # チャンネルに招かれたときの案内
        return "ここに書いたことをメモするよ。"

    async def on_message(self, req: Request, skill="", params=None) -> None:
        ts = await self.core.post(req.channel, "📌 " + req.text)
        self.core.records.put("memo", ts, {"text": req.text})
        await self.core.reply(req, "メモしたよ")  # 👀 を ✅ に変える

    async def run_schedule(self, name: str, day: str) -> dict:
        return {"status": "done", "count": len(self.core.records.items("memo"))}
```

| 差し込み口 | 呼ばれるとき |
|---|---|
| `on_message(req, skill, params)` | チャンネルへの書き込み。例外を投げたら ⚠️ を付けて知らせる |
| `welcome()` | チャンネルに招かれたとき |
| `run_schedule(name, day)` | 定期処理の時刻。`{"status": "error"}` を返すと朝の一覧に載る |
| `tick(now)` | 毎分（間隔はモジュールが決める） |
| `prepare(kind, day)` | Daily と振り返りの前。取り込み直す |
| `agenda(days, kinds)` | 朝の一覧・振り返りの予定（会議・授業・締切）。読めなければ `None` |
| `material(now)` | Daily と振り返りの材料 |
| `head_materials(days)` | 頭（手の口の道具）に渡す材料。種類 → 項目の dict（知識の `reading` など） |
| `on_reaction(event, added)` | リアクション。`True` を返すと、ほかには回らない |
| `on_event(kind, data)` | 出来事（下の表） |
| `on_slash_command(name, body)` | スラッシュコマンド。返した文は本人にだけ見える。3秒以内に返す |
| `on_action(name, body)` / `on_view(name, body)` | 投稿のボタン / 入力の画面。どちらも依頼者のときだけ |
| `home()` / `on_home_action(name, action)` | App Home の項目を作る / 押された |
| `head_action(name, params)` | 頭（手の口の道具）から頼まれた操作。Slack につながず App Home やボタンが無いときの入口（時間記録の `timer`、声の `voice`） |
| `on_start()` | 起動して Slack につながったあと |

| 出来事 | 配るところ | 中身 |
|---|---|---|
| `schedule` | 朝の一覧 | 1週間ぶんの予定 |
| `due` | 締切の24時間前（大学） | `title`・`at` |
| `working` / `done` / `failed` | 依頼を受けた・終わった・止まった | `theme` |
| `awaiting` | 返事待ちになった | `theme` |
| `limited` | AI の上限に当たった | `reset_at` |

| 窓口 `core` | 使えるもの |
|---|---|
| Slack | `post`・`mention`・`update`・`reply`・`react`・`upload`・`progress`・`open_view`・`update_view`・`permalink`・`channel_name`・`channels`・`thread_messages`・`watch_thread`・`claim_thread` |
| 担当と AI | `run_ai`（1回）・`work`（作業場で会話）・`converse`（担当と会話）・`ask_agent`・`tell_agent`・`pick_skill`・`ask_module` |
| 記録と設定 | `records`（`put`・`get`・`update`・`items`・`delete`）・`settings`・`schedule_detail`・`state_dir` |
| ボタンと App Home | `action_id`・`view_id`・`home_action_id`・`home_checkboxes`・`selected_values` |
| Daily・振り返り | `morning`・`mark_shown`・`digest`・`gather_prepare`・`gather_agenda`・`publish`・`last_ran` |
| そのほか | `owner_id`・`emit`・`spawn`・`notify_trouble`・`notice_once`・`sync_calendar`・`themes`・`provider`・`contains_secret`・`is_status_inquiry`・`checked_sections`・`final_answer` |
| 自分を直す（自己改善） | `repo_root`・`check_change`・`restart_for_update`・`last_update`・`busy` |

- `run_ai(用途, 文, req=)` は、答えをスレッドにも出す。返すのは答えの本文そのまま（Slack に出す部分は `final_answer(本文)`）
- 細かい引数は `src/kei_agent/api.py`
- 本体が Slack につながないとき（[architecture.md](architecture.md#頭と手)）、Slack の窓口の投稿（`post`・`reply`・`update`・`upload`・`publish` など）は知らせとしてたまり、頭が出す。`react`・`progress`・`open_view`・App Home は何もしない。`owner_id` は依頼者の ID

## agent.py

- `[process]` を持つモジュールの担当プロセス。見本は `modules/knowledge/agent.py`
- 起動は共通のコマンド `kei-agent-module <名前>`（launchd からは `deploy/run-agent.sh <名前>`）

```python
from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor, run_ai

SKILLS = [                                   # 名刺に載せる仕事。本体の振り分け係が読む
    AgentSkill(id="forecast", name="天気", description="本文の JSON（place）の明日の天気", tags=["weather"]),
    AgentSkill(id=ASK, name="天気の質問に答える", description="選択済み provider が答える", tags=["weather"]),
]


class Executor(SkillExecutor):               # self.config と self.store は土台が用意する
    async def handle(self, updater, metadata, text):
        if metadata.get("skill") == ASK:
            await self.answer(updater, text)          # 自由な質問は、ほかの担当と同じ形で
            return
        summary = await run_ai(self.config, self.store, "weather", "weather_brief", f"{text} の明日の天気を短く")
        await self.done(updater, summary, {"place": text})   # 断るなら self.fail(updater, 理由)
```

| 窓口 `kei_agent_a2a.api` | 中身 |
|---|---|
| `SkillExecutor` | `handle` を書く。`done`・`fail`・`answer`、`self.records` |
| `run_ai(config, store, 担当, 用途, 文)` | AI を1回動かして本文を返す。上限なら `AIError` |
| `progress(updater, {...})` | 経過を本体に流す |
| `ask_orchestrator` / `put_request` | 本体に中身を聞く / Slack の外から依頼を置く |
| `gateway_notion(名前)` / `Setup` | Notion（そのモジュールのホームだけ）/ DB を作る土台 |
| `settings(config, 名前)` / `records(config, 名前)` | 設定 / 記録（本体側と同じもの） |
| `json_object` / `json_list` | AI の答えから JSON を取り出す |
| `async background(executor)` | agent.py に書くと、担当と同じプロセスで動かし続ける |

- 手で動かすコマンドは `commands.py` に `COMMANDS = {"名前": main}`。`kei-agent-module <名前> <コマンド>` で動く（見本 `modules/time/commands.py`）

## テストの書き方（kei_agent.testing）

- `ModuleKit` が、モジュールを本番と同じ読み方の設定と、偽物の Slack・AI・Notion・担当と一緒に、本体の中で動かす
- 依頼者として頼み、Kei Agent が出した文を確かめる

```python
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]


async def test_it_answers_the_weather(module_kit):     # plugin の fixture
    kit = module_kit(MODULE, settings={"place": "東京"})
    kit.ai.answer("明日は晴れ")                       # AI が次に返す答え
    ts = await kit.message("明日の天気は？")           # チャンネルで頼み、終わるまで待つ
    assert kit.thread(ts) == ["明日は晴れ"]
    assert kit.ai.calls[0]["use_case"] == "weather_answer"
```

| できること | 使うもの |
|---|---|
| 依頼者として動く | `message`・`reply`・`invite`・`slash`・`action`・`view`・`schedule`・`tick`・`emit`・`home` |
| 出したものを見る | `texts()`・`thread(ts)`・`slack.calls`・`ai.calls`・`records` |
| 担当プロセスを試す | `kit.skill("仕事", {...})`。agent.py を同じプロセスで動かす（`LocalAgent`） |
| 担当を偽物にする | `local_agent=False` と `kit.agent.reply("仕事", "返事")`（`FakeAgent`） |

- テストはモジュールの `tests/` に置き、`kei-agent module test <名前>` で動かす
- テストの間は、本物の鍵を環境変数から外し、本物の状態・launchd・分類器・担当に触れない
- pytest で直接使うなら `-p kei_agent.testing.plugin`
