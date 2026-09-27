# 拡張できる構成（設計）

Kei Agent を、ほかの人が本体（コア）に触らずに、設定と自分のモジュールで作り替えられる形にする設計。
2026-09-26 に grilling で決めた（Q1〜Q33）。いまの作りは [`architecture.md`](architecture.md)。

## 目標

- ほかの人は、コアを触らず、設定と自分のモジュールだけで作り替えられる（fork で深く変えるのは止めない）
- 作り替えた人も、`git pull` でコアの更新をそのまま取り込める
- 担当や機能を足すときは「モジュールのフォルダを1つ足して、設定に1行書く」で済む（知識の担当を足したときは 42 ファイルを変えた）

## 前提

- 言葉は日本語（README の冒頭にだけ短い英語）
- 動かすのは macOS（launchd）。常駐のさせ方は1か所に閉じ込め、Linux（systemd）はあとで足せる形にする
- 名前は Kei Agent のまま。Slack に出る名前・一人称・口調は設定で変えられる
- 今の環境を止めずに段階的に作り替える。どの段も終わるたびにテストを通し、`deploy/update.sh` で反映する

## コアとモジュール

- **コアは枠だけ**
  - Slack の受け口と投稿、チャンネルからモジュールへの振り分け
  - AI を動かす仕組み（provider・モデル・制限の表）、保存（SQLite）
  - 定期処理・リアクション・App Home の枠、セットアップと更新のコマンド、常駐
- **機能はすべてモジュール**: 研究、大学、仕事、知識、声、Notion（ゲートウェイ）、Daily と振り返り、時間記録、自己改善、全体チャンネルの相談
  - 必須は Slack＋AI の provider 1つ（Claude か Codex）＋コアだけで、ほかはすべて任意
- **モジュールは機能のまとまり**。持てる部品は次のとおりで、担当プロセスもその1つにすぎない
  - チャンネル、定期処理、リアクション、App Home の項目、Notion の DB
  - 指示書と skill、担当プロセス（A2A）、AI の用途と制限の表の行
- **定義は `module.toml`＋ Python**
  - `module.toml`: 変わらない事実（名前、説明、担当プロセスの有無、要る秘密情報、使う連携、できること、チャンネルの既定の名前、定期処理の名前と既定の時刻、頼るモジュール、枠の版）
  - Python: 動きを決まった関数として書く。指示書と skill だけで済むモジュールは Python なしでも作れる
- 頼るモジュールは「必須」と「あれば使う」を分けて書ける（例: 知識は、Notion があれば先行研究 DB と読みもの DB に書き、無ければ Slack だけ）
- 朝の一覧や Daily は、各モジュールが材料を出す形にする（大学なら締切、仕事なら会議）
- 枠には版を付ける（`api = 1`）。合わない版のモジュールは読み込まず、setup・doctor・起動のときに理由を知らせる。枠を変えるときは版を上げ、変わった点と直し方を CHANGELOG に書く
- 使うモジュールは設定に並べて、はっきりオンにする（例: `modules = ["research", "knowledge", "my_weather"]`）
  - 組み込みのモジュールはリポジトリ直下の `modules/<名前>/`、自分のモジュールは自分のフォルダの `modules/<名前>/` に置き、どちらも同じ仕組みで読む（組み込みがそのまま作り方の見本になる）
- Python は `module.py` の `class Module` に、使う差し込み口のメソッドだけを書く（チャンネルへの書き込み、リアクション、朝の一覧に出す行、定期処理ごとの関数、招かれたときの案内など）
  - コアとのやり取りは、決めた範囲の窓口 `core`（`kei_agent.api.Core`）だけを通す（投稿する、担当に聞く、保存を読み書きする、Notion を読むなど）。`core` の範囲が、枠の版で約束する中身
- 担当プロセス（A2A のサーバー）のコードもモジュールのフォルダに入れる（`modules/<名前>/agent.py`）。起動は共通の1つのコマンドが、モジュールの名前を受け取って行う（`src/` や `pyproject.toml` に手を入れずに担当を足せる）

### module.toml の書き方（枠の版 1）

`modules/knowledge/module.toml` が見本。書けるのは次のキーだけで、知らないキーや形の違いは、読むときに断る（`src/kei_agent/modules.py`）。

```toml
api = 1                         # 枠の版。この Kei Agent と合わなければ読まない
name = "knowledge"              # フォルダの名前と同じ。英小文字・数字・-
label = "知識"                  # App Home などに出す名前
description = "…"

[depends]
requires = []                   # 必須のモジュール（設定の modules に無ければ断る）
optional = ["notion"]           # あれば使うモジュール

[actor]                         # AI の実行役（持つなら）。provider は App Home で選ぶ
prompt = "knowledge.md"         # 指示書（module.toml と同じフォルダ。利用者のフォルダの prompts/ に同じ名前を置けば差し替え）
files = "none"                  # none / read / write
shell = false
web = true
notion = "none"                 # none / read / write（Notion ゲートウェイ）
timeout_minutes = 10
default_use_case = "knowledge_answer"   # 自由な質問の用途（classify を書かなければ、分類器を動かさずにこれ）
# classify = "1件の要点は work_single_source、複数の状況要約は work_cross_source。"
#                               # 自由な質問の用途を、軽いモデルで選び分けるときの見分け方（Web を使う用途が2つ以上）
# plugin = true                 # skill と二の柵のフック（同じフォルダの plugin/。.claude-plugin/plugin.json が要る）
# workspace = "~/course"        # 作業場（無ければ状態の置き場の agents/<名前>）。同じフォルダに CLAUDE.template.md が
#                               # あれば、はじめて使うときに作業場の CLAUDE.md として置く（前提のメモを書く場所）

# [[actor.connectors]]          # アカウントの連携。書いた道具だけを使える（読む道具だけを書く）
# name = "outlook"
# claude_server = "claude_ai_Microsoft_365"         # claude.ai のコネクタ（mcp__<server>__<道具>）
# claude_tools = ["outlook_calendar_search", "outlook_email_search"]
# [[actor.connectors.codex_apps]]                   # Codex の App（表示名と、道具の名前空間）
# name = "Microsoft Outlook Calendar"
# namespace = "microsoft_outlook_calendar"
# tools = ["search_events", "list_events"]

[use_cases.knowledge_pick]      # 用途ごとのモデル。コアのモデルの一覧の中からだけ選べる
offline = true                  # Web を使わない回（外の文を材料として渡す回）
claude = { model = "claude-haiku-4-5" }
codex = { model = "gpt-6-luna", effort = "low" }
# manual = true                 # 依頼の頭に [[knowledge-pick]] と書いたときだけ使う用途（分類器は選ばない）。
#                               # いちばん強いモデル（claude-fable-5・gpt-6-astra）は、この用途でしか書けない

[process]                       # 常駐のプロセス（持つなら）
port = 8792                     # 127.0.0.1 のこの番地。設定の [a2a.agents] に書かなければ、ここを使う
# kind = "service"              # A2A の担当ではない口（agent.py の代わりに service.py の serve(config, port)。
#                               # Notion のゲートウェイ）。/health で {"version": RUNNING_VERSION} を返す

[channels]                      # チャンネルの種類 = 既定の名前（番号を外した名前。設定の [channels] で変えられる）
knowledge = ["knowledge"]
# theme = ["*"]                 # ほかのどれにも当たらないチャンネル（研究テーマ）を受け持つ。受け持てるのは1つのモジュールだけ

[schedules.reading]             # 定期処理。時刻は設定の [schedule] と App Home で変えられる
label = "読みもの"
short = "読みもの"              # App Home のチェックに出す短い名前
default = "07:00"               # 空文字なら、既定では動かさない

# [slash_commands]              # Slack のスラッシュコマンド（/ を付けない名前 = 説明）。Slack の App にも同じ名前で足す
# stamp = "スタンプを押す"      # 打たれると class Module の on_slash_command(name, body)

# [settings]                    # 設定（持つなら）。名前 = 既定の値。利用者は config.toml の、モジュールの名前の表で変える
# school = ""                   # （大学なら [course] school = "waseda"）。値は既定と同じ形（文字・数・真偽・配列・表）にする
```

- 用途・定期処理・チャンネルの種類・番地は、モジュールどうしでぶつかってはいけない（ぶつかれば設定を読むときに断る）
- 同じ時刻なら、夜間の Task → モジュールの定期処理（設定の modules の順）→ Daily → 振り返り → 保守の順に動く
- 担当プロセスの名前（`deploy/install.sh <名前>` など）は、`[process]` を持つモジュールから起動スクリプトが見つける
- 設定は `config.toml` の、モジュールの名前の表（大学なら `[course]`）に書く。書けるのは `[settings]` にある名前だけで、形が既定と違えば読むときに断る。モジュールをオフにしても、設定は消さずに残しておける。本体の設定の表（`[notion]` など）と同じ名前のモジュールは、設定を持てない
- `[channels]` か `[schedules]` を書いたら、同じフォルダに `module.py` が要る。`[process]` なら `agent.py`（`kind = "service"` なら `service.py`）、`[actor]` なら `prompt` の指示書（どれも、無ければ設定を読むときに断る）
- `kind = "service"` のプロセスは担当ではないので、本体は A2A でつながない。起動し直すときは担当より先にし、反映のあとは `/health` の版を確かめる

### module.py の書き方（枠の版 1）

`modules/knowledge/module.py` が見本。Kei Agent の部品で読み込んでよいのは窓口の `kei_agent.api` だけで、同じフォルダのファイルは `from . import texts` のように読める。本体が起動するときに読み込み（`modules.load_code`）、書いた差し込み口だけを呼ぶ。

```python
from kei_agent.api import Core, Request


class Module:
    default_question = "今日のメモを見せて"      # 本文の無いメンションのときに、担当に聞くこと

    def __init__(self, core: Core):
        self.core = core

    def welcome(self) -> str:                    # チャンネルに招かれたときの案内
        return "ここに書いたことをメモするよ。"

    async def on_message(self, req: Request, skill="", params=None) -> None:   # [channels] があれば必須
        ts = await self.core.post(req.channel, "📌 " + req.text)
        self.core.records.put("memo", ts, {"text": req.text})
        await self.core.reply(req, "メモしたよ")               # 👀 を ✅ に変える

    async def run_schedule(self, name: str, day: str) -> dict:  # [schedules] があれば必須
        return {"status": "done", "count": len(self.core.records.items("memo"))}

    def morning_notes(self, day: str) -> list[str]:            # 朝の一覧に足す行
        detail = self.core.schedule_detail("tidy", day)
        return [f"メモ: {detail['count']}件"] if detail else []

    async def on_reaction(self, event: dict, added: bool) -> bool:
        return False                             # 自分の投稿へのものなら扱って True
```

| 差し込み口 | 呼ばれるとき |
|---|---|
| `on_message(req, skill, params)` | モジュールのチャンネルと、`claim_thread` したスレッドへの依頼者の書き込み。研究全体のチャンネルから回ってきたときは、振り分け係が選んだ仕事が `skill` と `params` に入る（空なら `core.pick_skill(req)` で選べる）。答えは `core.reply` か `core.converse` で返す。例外を投げたら ⚠️ を付けて知らせる |
| `run_schedule(name, day)` | `[schedules]` の時刻。返した辞書は記録に残り、`{"status": "error"}` なら朝の一覧の「うまくいかなかったこと」に載る |
| `morning_notes(day)` | 朝の一覧（Daily の投稿）を作るとき |
| `agenda(days, kinds)` | 朝の一覧・振り返りの材料・振り返りのスレッドの締切を作るとき。これから `days` 日の、時刻のある予定を返す。種類は会議 `{"kind": "meeting", "subject", "start", "end", "location", "url", "id", "source"}`、授業 `{"kind": "class", "subject", "start", "end"}`、締切 `{"kind": "due", "title", "course", "at", "url", "id", "notice"}`。`kinds` が来たら、その種類だけでよい（締切だけのときに会議を AI で読まない）。会議は共通ホームの予定カレンダーにも `source` を出典として書く。朝に出した締切の `notice` は目印として記録する（24時間前の知らせで `core.notice_once(notice)` を使えば繰り返さない）。読めなかったら `None`（空の一覧と分けて、予定カレンダーの行を「要確認」にしない） |
| `prepare(kind, day)` | Daily（`"daily"`）と振り返り（`"review"`）の前。データを取り込み直す（大学なら Moodle の課題）。うまくいかなかったことの短い名前の一覧を返すと、朝の一覧の「うまくいかなかったこと」に載る |
| `tick(now)` | 毎分。間隔はモジュールが決める（締切の24時間前の知らせを1時間に1回見る、など） |
| `on_reaction(event, added)` | リアクションの付け外し。True を返したら、ほかのモジュールと 🌙 には回らない |
| `on_event(kind, data)` | 本体やほかのモジュールが出来事を配ったとき（`core.emit`。種類は下の「出来事」）。投げっぱなしなので返事は要らない。落ちても配った側は止まらない |
| `home()` | App Home を作るとき。このモジュールの項目（Slack の blocks）を返すと、表示名の見出しの下に並ぶ。押せるものの action_id は `core.home_action_id(名前)`、チェックは `core.home_checkboxes(名前, 値→表示名, 付いている値)` で作る。依頼者にだけ出る |
| `on_home_action(name, action)` | App Home の、このモジュールの項目が押されたとき（`name` は action_id を作ったときの名前、`action` は Slack の action。選ばれた値は `selected_values(action)`）。終わると App Home を作り直す |
| `on_slash_command(name, body)` | `[slash_commands]` のコマンドが打たれたとき。返した文を、打った人にだけ見せる。Slack は3秒以内の返事を求めるので、時間のかかることは `core.spawn` に回す |
| `on_action(name, body)` | このモジュールの投稿のボタンなど（action_id は `core.action_id(名前)`）が押されたとき |
| `on_view(name, body)` | このモジュールの入力の画面（callback_id は `core.view_id(名前)`）が送られたとき。欄の下に出す理由を `{block_id: 文}` で返すと、画面を閉じない |
| `material(now)` | Daily と振り返りの材料を作るとき。足す行を返す（今週の時間など） |
| `welcome()` | モジュールのチャンネルに Kei Agent が招かれたとき |

コマンド・ボタン・入力の画面は、依頼者のときだけモジュールに渡る。

出来事（`core.emit(種類, 中身...)` で配り、`on_event(種類, 中身)` で受け取る。空の中身は外して渡す）:

| 種類 | 配るところ | 中身 |
|---|---|---|
| `schedule` | 朝の一覧（Daily）を作ったとき | `items`（1週間ぶんの予定。1件は `date`・`at`・`end`・`icon`・`text`） |
| `due` | 締切まで24時間を切ったとき（大学） | `title`・`at` |
| `working` / `done` / `failed` | 依頼を受けた・終わった・止まった | `theme`（チャンネルの名前） |
| `awaiting` | 依頼者の返事待ちになった | `theme` |
| `limited` | AI の契約の上限に当たった | `reset_at`（明ける時刻） |

窓口 `core` でできること（`src/kei_agent/api.py`）:

- Slack: `post`（ts を返す。`blocks=` でボタンも置ける）、`update`（自分の投稿を書き換える）、`open_view` / `update_view`（入力の画面）、`permalink`、`channel_name`（番号つきの名前。番号を外したテーマの名前は `theme_name(名前)`）、`action_id` / `view_id`、`reply`（`failed=True` なら ⚠️）、`react`、`channel_ids`、`channels(種類)`、`is_owner`、`watch_thread`（メンションなしの返信を拾う）、`claim_thread`（研究テーマのチャンネルでも、そのスレッドの続きを受ける）。失敗の決まった文は `failure_text()`
- 担当: `work(req)`（研究テーマのチャンネルで、そのチャンネルの作業場を使って担当と会話して答える。添付・できたファイル・接続先の許可・引き継ぎ・ジョブは研究と同じ流れ）、`ask_agent(skill, 材料)`（`[process]` の担当に仕事を頼む）、`tell_agent(skill, 材料)`（AI を動かさない知らせだけを渡す。届かなくても困りごととして知らせない）、`converse(req)`（担当と会話として答える）、`pick_skill(req)`（言われたことが名刺のどの仕事かを軽いモデルで選ぶ。選べなければ `ASK`）
- 記録: `records`（`put` / `get` / `update` / `items` / `delete`。種類と鍵で1件、中身は JSON にできる辞書。`keep_days` を付けたものは、その日数で毎晩の保守が消す）、`schedule_detail(名前, 日付)`
- 設定: `settings`（module.toml の `[settings]` の既定に、config.toml の `[<名前>]` を重ねた写し）
- 予定カレンダー: `sync_calendar(出典, 予定, day=, days=, complete=, expected_count=)`（出典と ID で照合し、手入力の行には触らない。見えなくなった行は消さずに「要確認」）
- 出来事と App Home: `emit(種類, 中身...)`（出来事を配る）、`home_action_id(名前)`・`home_checkboxes(名前, 値→表示名, 付いている値)`（App Home の項目）、`selected_values(action)`（押された項目の、選ばれた値）
- ほかのモジュール: `ask_module(名前, skill, 材料)`（`[depends]` に書いたモジュールの担当に頼む）
- Toggl: `load_toggl` / `Toggl` / `TogglError` / `TogglAmbiguousWrite`（窓口の kei_agent.api から）
- そのほか: `spawn`（裏で動かす）、`notify_trouble`、`notice_once`、`themes()`（研究テーマの名前・場所・検索キーワード・前提）、`to_thread`（時間のかかる読み書き）。Notion は、Notion のモジュールができるまで `hub`（共通ホーム）と `notion`（研究ホーム）をそのまま渡す

### agent.py の書き方（枠の版 1）

`[process]` を持つモジュールの担当プロセス（A2A のサーバー）。`modules/knowledge/agent.py` が見本。Kei Agent の部品で読み込んでよいのは窓口の `kei_agent_a2a.api` だけで（本体側の `kei_agent.api` とは別）、同じフォルダのファイルは `from . import digest` のように読める。起動は共通のコマンド `kei-agent-module <名前>` が、module.toml の番地で行う（launchd からは `deploy/run-agent.sh <名前>`。依存は担当に共通の `agents` のグループ）。

```python
from kei_agent_a2a.api import ASK, AgentSkill, SkillExecutor, run_ai

DESCRIPTION = "明日の天気を調べる"          # 名刺の説明（無ければ module.toml の description）
SKILLS = [                                   # 名刺に載せる仕事。本体の振り分け係が読む
    AgentSkill(id="forecast", name="天気", description="本文の JSON（place）の明日の天気を返す", tags=["weather"]),
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

窓口 `kei_agent_a2a.api` でできること（`src/kei_agent_a2a/api.py`）:

- `SkillExecutor`（`handle` を書く。`done` / `fail` / `answer`。`self.records` はこのモジュールだけの記録で、本体側の `core.records` と同じもの）、`AgentSkill`、`ASK`、`progress(updater, {"activity": …})`（経過を本体に流す）
- 研究テーマを受け持つ担当（`core.work`）には、`ask` の依頼に `channel_name`（作業場）と `allowed_domains`（許可済みの接続先）が添えて届く。作業場は `SkillExecutor.workspace(ask)` を書き換えて決める
- `async background(executor)`（agent.py に書けば）: 担当と同じプロセスで動かし続ける仕事（声ならマイクの会話）。起動のときに始まり、止めるときに止まる
- `ask_orchestrator(config, 担当, 質問, theme=…)`: 本体の問い合わせ口に、研究・大学・仕事の中身を聞く（担当を呼べるのは本体だけ。本体が読むだけで聞き、Slack と同じ出力の確認を通した答えを返す。聞けなければ `OrchestratorError`）。`put_request(config, テーマ, 文, note=…)`: Slack の外から依頼を置く（本体が拾ってテーマのチャンネルにスレッドを立てる）
- `run_ai(config, store, 担当, 用途, 文, provider=…, prompt_file=…, profile=…)`: その担当の用途（`[use_cases]`）で AI を1回動かし、答えの本文を返す。作業場は読むだけ。上限に当たったら `AIError`（`limit_reset_at` つき）
- `workspace(config, 担当)`（覚えておきたいものを置く作業場）、`json_object` / `json_list`（AI の答えから JSON を取り出す）、`requested_days(本文, 既定)`（本文の JSON の days）
- Notion（ゲートウェイ経由。`gateway_notion(名前)` の名前で届くホームが決まる）と `Setup`（DB を作る setup の土台）、Toggl（`load_toggl`）、日付（`WEEKDAYS`・`weekday`・`day_label`・`parse_time`）、`load_config`、`settings(config, 名前)`（そのモジュールの設定。本体側の `core.settings` と同じもの）

手で動かすコマンド（setup など）は、同じフォルダの `commands.py` に `COMMANDS = {"名前": main(argv)}` を置く。`kei-agent-module <名前> <コマンド> [引数...]` で動く（`pyproject.toml` にコマンドの名前を足さない）。コマンドからモジュールの記録を読み書きするのは `records(config, 名前)`（本体側の `core.records` と同じもの）、本体の代わりにボタンつきの投稿を置くなら、ボタンの名前は `action_id(名前, ボタン)`（本体側の `core.action_id` と同じ。押されると本体側の `on_action`）。見本は `modules/time/commands.py`（時間記録のカードを置く）。

## 設定と置き場所

- **自分のものは `~/.config/kei-agent/`**（場所は環境変数 `KEI_AGENT_HOME` で変えられる）
  - `config.toml`: 設定の本体。リポジトリには `config.example.toml` だけを置く
  - `profile.md`: プロフィール（呼び方・一人称・口調・所属・興味）。指示書に差し込む
  - `prompts/`: 指示書の差し替え。同じ名前のファイルがあれば、リポジトリの指示書の代わりに使う
  - `modules/`: 自分のモジュール
  - `themes.toml`: 研究テーマと置き場所の対応
  - `secrets/`: 秘密情報
- 状態（SQLite など）は今どおり `~/.local/state/kei-agent/`
- **構造は設定ファイルが正本**。App Home は日々の操作だけ（担当ごとの AI、定期実行のオン・オフと時刻、声、接続先）
- **秘密情報**は、共通のもの（`kei-agent.zsh`）とモジュールごとのもの（`kei-agent-<名前>.zsh`）に分ける
  - ほかのモジュールの鍵は、そのプロセスに載らない
  - 場所は設定の `[paths] secrets` で変えられる。作者の環境は `~/.config/zsh/local` を指す
  - 秘密情報の場所は、いつも AI に読ませない場所に入る
- 担当ごとに別の AI アカウントを使える（大学は個人、仕事は会社のアカウント、のように）

## AI・連携・安全

- **モデル**
  - 使ってよいモデルの一覧は、コアに置く
  - 各モジュールは、自分の用途と既定のモデルを定義に書く（一覧の中からだけ選べる）
  - 使う人は、設定でモジュールごとに上書きできる
- **連携（MCP など）**
  - モジュールは、使う連携と許可する道具を定義に書く（例: 大学＝Box を読むだけ）
  - 使う人は、つなぎ方（Claude のコネクタ／Codex のアプリ／自分の MCP サーバー）を設定で選ぶ
  - モジュールに無い MCP は、設定にモジュールごとのサーバーと許可する道具を書けば足せる
- **安全**
  - モジュールはできること（ファイル・コマンド・Web・Notion・連携の道具・Slack に出すか）を宣言する
  - 宣言を超えることは、コアの制限の表が実行のときに止める
  - 「外の文・個人のデータ・外へ出す口の3つを同じ回に揃えない」は、コアがモジュールの定義で確かめ、揃うモジュールは setup で断る
  - オンにするとき、setup はモジュールのできることを一覧で見せて確認を取る

## 研究テーマの置き場所

- 研究はモジュール（`modules/research/`）。ほかのどれにも当たらないチャンネルを研究テーマとして受け持ち（`[channels]` に `"*"`）、`core.work` でテーマの作業場で答える。担当プロセスはテーマの作業場で AI を動かし、長い処理（pueue のジョブ）の待ち行列も持つ
- 研究をオフにすると、研究テーマのチャンネルと研究全体の相談には答えない（受け持つモジュールが無いと伝える）。自分のモジュールに `"*"` を持たせれば、研究の代わりに受け持たせられる

- **置き場所の選び方**
  - 全テーマの既定の置き場所は、setup で選ぶ（いまは config.toml の `research_root`）
  - テーマごとに、既存のフォルダやリポジトリも使える。招いたときに [既定の場所に作る] [既存のフォルダを使う] と聞き、`themes.toml` でも変えられる
  - `themes.toml`（利用者のフォルダ）は「テーマの名前 = フォルダ」。変わると読み直すので、起動し直さなくてよい。使えない場所（無い、ホームやディスクの一番上、既定の置き場所・Kei Agent 自身・状態・設定・秘密情報の中、許可していない保護フォルダ）は、起動のときに理由を出して止め、Slack の入力では欄の下に理由を出す（`src/kei_agent/themes.py`、`theme_invite.py`）
- **既存のリポジトリには、なるべく触らない**
  - `CLAUDE.md` があれば、そのまま前提として使う。無いときだけひな形を作る
  - スレッドの記録やジョブの状態は `.kei-agent/` に集め、`.git/info/exclude` に入れる
  - `inputs/` `outputs/` `logs/` は、初めて使うときに作る
  - Git の操作は今の柵のまま（push はしない）
- **保存**: 毎晩の保存（Git へのコミット）は、既定の置き場所だけにする。外の場所のテーマは、その人の Git に任せる（招いたときに伝える）
- **保護フォルダ**（書類・デスクトップ・ダウンロード）
  - 既定では選べない
  - 使いたい人は、設定で明示的に許可する（`allow_protected_folders = true`）。setup はフルディスクアクセスの手順と影響を示す（保護フォルダ全体が読めるようになる、Python の更新のたびに許可し直し。いまは deploy/README.md の「保護フォルダ」）
  - iCloud で同期している場所には、警告を出す

## 大学・Notion・指示書

- **大学**: 大学モジュールは汎用にし、学校ごとの違いは部品にする。早稲田はその1つとして同梱する
  - 設定にするもの: 時限の時刻、学期
  - 部品にするもの: 成績の取り込み方
  - 設定は `config.toml` の `[course]`: `school`（学校の部品）、`periods`（時限の時刻）、`terms`（学期と、開かれる月）。時限と学期は、書かなければ学校の部品の既定を使う。学校を選ばず時刻も書かなければ、授業に時刻が付かない（朝の一覧に出ない）
  - 学校の部品は `modules/course/schools/<名前>.py`（同梱は `waseda`）。自分で書いた部品は、そのファイルの場所を `school` に書く。部品に置けるもの（時限・学期の既定、成績の読み方、成績と授業・GPA・単位要件の対応）は `modules/course/school.py` に書いてある
  - 個人の履修科目はコードに持たず、自分のフォルダのファイルから入れる（`kei-agent-module course setup --seed <ファイル>`。書き方は `modules/course/courses.example.toml`）
- **時間記録**: 時間記録はモジュール（`modules/time/`）。`/toggl` と固定したカードで測り、Toggl と共通ホームの「時間記録」に送る。Toggl のアプリで直接測った記録の取り込みは、そのモジュールの定期処理（「Toggl の取り込み」、既定 22:00）
  - 設定は `config.toml` の `[time]`: `prefixes`（時間を測るチャンネルの名前の頭 → 領域。既定は `10_` 研究・`20_` 大学・`30_` 仕事）、`pick_course`（始めるときに今学期の科目を選ぶチャンネル。大学のモジュールに聞く）
  - 計測の記録はモジュールの記録に置く。本体が持っていたころの表（`time_entries` など）からは一度だけ写し、表は念のため残してある。前のボタンの名前のままのカードは、起動して最初の見回りで描き直す（消されていたカードは置き直さずに忘れる）
  - カードを置くのは `kei-agent-module time cards`（カードのあるチャンネルには置かない）
- **Notion**: 今の DB の形（名前・列）を「標準の形」として固定し、setup が作る。使うかどうかは任意
  - ホームのページ ID は `config.toml` の `[notion]` にまとめる（研究は `research_home`、大学は `course_home`、ほかのモジュールは `[notion.homes]` に「名前 = ページ ID」）。ホームを書いたモジュールは、自分の名前の合言葉で、そのホームの下だけに届く（AI の道具も、Python の `gateway_notion(名前)` も）
  - ホームの無い担当には Notion の道具を渡さない。シェルを使える AI の実行役がいるモジュールは、Python からの `/notion/v1`（何でも送れる口）を使えず、MCP の道具だけ
- **指示書**
  - コアの指示書は汎用に書き、個人の前提はプロフィールから差し込む
  - 細かく変えたい人は、指示書を丸ごと差し替えられる

## セットアップと、作る人への支え

- **セットアップ**
  - `kei-agent setup`（対話）: 呼び方と口調 → 使うモジュール → AI の provider → Slack App（選んだモジュールに合わせた manifest を作る）→ 秘密情報 → Notion → 常駐の登録 → 動くかの確かめ
  - 作ったものはただのファイルなので、あとは直接書き換えてよい
  - `kei-agent doctor` で確かめ、`kei-agent module add <名前>` で足す
- 反映は `deploy/update.sh`（main で、依存をそろえ、変わった plist だけ登録し直し、全部を起動し直し、版を確かめる）
- **作る人への支え**
  - `kei-agent module new <名前>` で、ひな形（定義・Python・指示書・テスト）を作る
  - テスト用の偽物（Slack・Notion・AI・担当）を `kei_agent.testing` として出す
  - GitHub Actions で、組み込みのモジュールとひな形のテストを毎回走らせる

## 段階

| 段 | やること | 状態 |
|---|---|---|
| 1 | 個人のものを `~/.config/kei-agent/` に出す（設定・プロフィール・指示書の差し替え・秘密情報の場所）。リポジトリには例の設定だけを残す | 済み（2026-09-26） |
| 2 | モジュールの枠。まず知識を載せ替えて形を確かめる。3つに分けて反映する: ① 定義と読み込み（`module.toml` から、担当の名前・表示名・用途とモデル・制限の表の行・チャンネルと定期処理の既定・担当プロセスの番地を作る）② 差し込み口と `core`（チャンネル・定期処理・リアクション・朝の一覧・招かれたときの案内。知識の本体側を `modules/knowledge/module.py` へ）③ 担当プロセスを `modules/knowledge/agent.py` へ移し、共通の起動コマンドで動かす | 済み（2026-09-27） |
| 3 | 残りを載せ替える（仕事 → 大学（学校の部品化と早稲田）→ 声 → Notion → 研究（テーマの置き場所を含む）→ Daily・振り返り・時間記録・自己改善）。仕事は2つに分ける: ① 枠を広げる（連携の道具・用途の選び分け・plugin・振り分けの受け渡し・予定の agenda・声）② 仕事を `modules/work/` へ | 仕事の①②済み（2026-09-27）。大学は ① 枠を広げる（見回り tick・取り込み prepare・予定の種類・予定カレンダーの窓口・モジュールのコマンド・担当側の窓口）② 大学を `modules/course/` へ ③ 学校ごとの違いを部品にする（早稲田はその1つ）。大学の①②③は済み（2026-09-27）。声は ① 枠を広げる（出来事の受け口・App Home のモジュールの項目・担当側の常駐の仕事と窓口）② 声を `modules/voice/` へ。①②は済み（2026-09-27）。Notion は ① ホームをモジュールごとに持てるようにする（`[notion.homes]`、ゲートウェイの利用者をホームから決める）② ゲートウェイを `modules/notion/` へ（A2A ではない常駐のプロセス `kind = "service"`）。①②は済み（2026-09-27）。研究は ① 枠を広げる（用途の手動指定 `[[名前]]` と `manual`、ほかのどれにも当たらないチャンネル `"*"`、チャンネルの作業場での会話 `core.work`）② 研究を `modules/research/` へ ③ テーマの置き場所（`themes.toml`、招いたときの選択、既存のフォルダへの配慮、保護フォルダ）。①②③は済み（2026-09-27）。時間記録は ① 枠を広げる（スラッシュコマンド、投稿のボタンと入力の画面、`ask_module`、Daily の材料）② 時間記録を `modules/time/` へ。①は済み（2026-09-27）、②は反映待ち |
| 4 | `kei-agent setup` / `doctor` / `module add`、Slack の manifest の生成、常駐の登録 | |
| 5 | README、モジュールの作り方の文書、`kei-agent module new`、`kei_agent.testing`、GitHub Actions | |

過去のコミットに残っている個人の値（Notion のページ ID、学校名）は、履歴を書き換えずにそのまま残す。鍵やトークンは入っていないことを確かめた（2026-09-26）。
