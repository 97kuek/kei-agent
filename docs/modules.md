# モジュールの作り方

Kei Agent の機能は、どれもモジュール（`modules/<名前>/`）でできている。自分のモジュールを足せば、コアを直さずに担当や定期処理を増やせる。組み込みのモジュール（リポジトリの `modules/`）は、そのまま作り方の見本になる。設計の決めごとは [`docs/extensibility.md`](extensibility.md)、仕組みは [`docs/architecture.md`](architecture.md)。

## はじめる（作る → 試す → オンにする）

1. ひな形を作る: `uv run kei-agent module new weather --ai --label 天気`
   - 自分のフォルダ（`~/.config/kei-agent/modules/weather/`）に、`module.toml`・`module.py`・`weather.md`（指示書）・`tests/test_weather.py` ができる。担当プロセスも持たせるなら `--process`（`agent.py` と、空いている番地）
   - 設定（`config.toml`）は、まだ変えない
2. 中身を書く: `module.toml` の `description`、`module.py` の答え方、`weather.md` の受け持ち（書けることは下の「module.toml の書き方」「module.py の書き方」）
3. 試す: `uv run kei-agent module test weather`（担当プロセスがあるなら `uv run --group agents kei-agent module test weather`）。本物の Slack・AI・秘密情報には触れない（下の「テストの書き方」）
4. オンにする: `uv run kei-agent module add weather`。設定の `modules` に足し、そのあとにやること（起動し直す、チャンネルを作って Kei Agent を招く、App Home で AI を選ぶ、manifest の貼り直し）を並べる
5. 確かめる: `uv run kei-agent doctor`。Slack の `#weather` で `@Kei Agent 明日の天気は？`

Kei Agent に足して公開するときは、`--builtin` でリポジトリの `modules/` に作り、プルリクエストにする（[`CONTRIBUTING.md`](../CONTRIBUTING.md)）。

## フォルダの中身

| ファイル | 役目 | 要るとき |
|---|---|---|
| `module.toml` | 定義（名前・チャンネル・定期処理・AI の実行役・担当プロセス・設定・秘密情報） | いつも |
| `module.py` | 本体側の動き（`class Module` の差し込み口） | チャンネル・定期処理・コマンドなどを持つとき |
| `<名前>.md` など | AI の実行役の指示書（`[actor] prompt`。利用者のフォルダの `prompts/` に同じ名前を置けば差し替え） | `[actor]` があるとき |
| `agent.py` | 担当プロセス（`SKILLS` と `class Executor`） | `[process]` があるとき |
| `service.py` | A2A ではない常駐のプロセス（`serve(config, port)`） | `[process] kind = "service"` のとき |
| `commands.py` | 手で動かすコマンド（`COMMANDS`。`kei-agent-module <名前> <コマンド>`） | 任意 |
| `plugin/` | skill と二の柵のフック（Claude Code の plugin） | `[actor] plugin = true` のとき |
| `CLAUDE.template.md` | 作業場に最初に置く CLAUDE.md（前提のメモ） | 任意 |
| `tests/` | テスト（`kei-agent module test <名前>`） | 任意（ひな形に入っている） |

## 決まりごと

- 読み込んでよい Kei Agent の部品は窓口だけ: `module.py` は `kei_agent.api`、そのほかのファイル（`agent.py` など）は `kei_agent_a2a.api`。同じフォルダのファイルは `from . import texts` のように読める
- 枠の版（`api = 1`）: 合わない版のモジュールは読み込まない（起動・`doctor`・`setup` で理由を知らせる）
- できることの宣言（`[actor]` の `files`・`shell`・`web`・`notion`・連携の道具）を超えることは、コアの制限の表が実行のときに止める。外の文（記事・論文）を材料に渡す用途は `offline = true` にして、外の文・個人のデータ・外へ出す口を1回に揃えない
- AI に書かせてよいのは、自分のフォルダ（`core.state_dir`）か、`[actor] files = "write"` のときの作業場だけ
- 秘密情報は `[secrets]` に名前と説明だけを書く。値はコード・ログ・Slack に出さない（公開の場所に書く前は `core.contains_secret` で確かめる）
- 指示書の返答は、`<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間だけが Slack に出る（印が無いと答えが空になる）。作業手順・道具の名前・手元のパスは出さない
- 名前は英小文字・数字・-（フォルダの名前と同じ）。用途・定期処理・チャンネルの種類・番地は、ほかのモジュールとぶつけない（ぶつかれば設定を読むときに断る）

## module.toml の書き方（枠の版 1）

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

# core_channels = ["improve"]   # 会話を受け持つ本体のチャンネル（[channels] より前、トップに書く）。improve は Kei Agent の
#                               # チャンネル（名前は設定の [channels] improve）。受け持てるのは1つのモジュールだけで、困りごとの
#                               # 知らせは受け持つモジュールが無くても本体が出す
# core_schedules = ["daily", "review"]   # 受け持つ本体の定期処理（トップに書く。run_schedule に届く）。時刻は設定の
#                               # [schedule] daily / review と App Home のまま、順番も今のまま（モジュールの定期処理のあと）

[schedules.reading]             # 定期処理。時刻は設定の [schedule] と App Home で変えられる
label = "読みもの"
short = "読みもの"              # App Home のチェックに出す短い名前
default = "07:00"               # 空文字なら、既定では動かさない

# [slash_commands]              # Slack のスラッシュコマンド（/ を付けない名前 = 説明）。kei-agent manifest が Slack App の
#                               # manifest に載せる（貼り直して入れ直す）
# stamp = "スタンプを押す"      # 打たれると class Module の on_slash_command(name, body)
# toggl = { description = "時間記録を始める・止める", usage_hint = "[start|stop]" }   # 打ち方の例も載せるとき

# [settings]                    # 設定（持つなら）。名前 = 既定の値。利用者は config.toml の、モジュールの名前の表で変える
# school = ""                   # （大学なら [course] school = "waseda"）。値は既定と同じ形（文字・数・真偽・配列・表）にする

# [secrets]                     # 要る秘密情報（環境変数の名前 = 説明と扱い。値は書かない）。kei-agent setup が聞いて秘密情報の
#                               # ファイルに書き、kei-agent doctor が有無を確かめる（本体のものは modules.CORE_SECRETS）
# WEATHER_API_KEY = { description = "天気の API のキー", required = true, own_file = true }
#                               # required: 無いと動かない（書かなければ任意）。own_file: そのプロセスだけのファイル
#                               # kei-agent-<名前>.zsh に置く（[process] を持つモジュールだけ）。generate = true なら setup が
#                               # 値を作る（プロセスどうしの合言葉）。group = "Toggl" の任意の鍵は、そろって初めて使う
```

- 用途・定期処理・チャンネルの種類・番地は、モジュールどうしでぶつかってはいけない（ぶつかれば設定を読むときに断る）
- 同じ時刻なら、夜間の Task → モジュールの定期処理（設定の modules の順）→ Daily → 振り返り → 保守の順に動く
- 担当プロセスの名前（`deploy/install.sh <名前>` など）は、`[process]` を持つモジュールから起動スクリプトが見つける
- 設定は `config.toml` の、モジュールの名前の表（大学なら `[course]`）に書く。書けるのは `[settings]` にある名前だけで、形が既定と違えば読むときに断る。モジュールをオフにしても、設定は消さずに残しておける。本体の設定の表（`[notion]` など）と同じ名前のモジュールは、設定を持てない
- `[channels]`・`core_channels`・`core_schedules`・`[schedules]` を書いたら、同じフォルダに `module.py` が要る。`[process]` なら `agent.py`（`kind = "service"` なら `service.py`）、`[actor]` なら `prompt` の指示書（どれも、無ければ設定を読むときに断る）
- `kind = "service"` のプロセスは担当ではないので、本体は A2A でつながない。起動し直すときは担当より先にし、反映のあとは `/health` の版を確かめる

## module.py の書き方（枠の版 1）

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
| `on_message(req, skill, params)` | モジュールのチャンネル・`core_channels` の本体のチャンネルと、`claim_thread` したスレッドへの依頼者の書き込み。研究全体のチャンネルから回ってきたときは、振り分け係が選んだ仕事が `skill` と `params` に入る（空なら `core.pick_skill(req)` で選べる）。答えは `core.reply`・`core.converse`・`core.work` で返す。例外を投げたら ⚠️ を付けて知らせる |
| `run_schedule(name, day)` | `[schedules]` と `core_schedules` の時刻。返した辞書は記録に残り、`{"status": "error"}` なら朝の一覧の「うまくいかなかったこと」に載る |
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
| `on_start()` | 起動して Slack につながったあと。Kei Agent を入れ替えたあとの起動なら、その結果は `core.last_update()`（途中で止まった作業の後始末や、入れ替えの結果の知らせに使う） |
| `welcome()` | モジュールのチャンネル（と `core_channels` の本体のチャンネル）に Kei Agent が招かれたとき |

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

- Slack: `post`（ts を返す。`blocks=` でボタンも置ける。`markdown=True` なら Markdown で書ける）、`update`（自分の投稿を書き換える）、`upload`（スレッドにファイルを添付する）、`thread_messages` / `thread_history`（スレッドの投稿と、AI に渡す文にしたもの）、`progress(req, 文)`（その間、入力欄の下に経過を出す）、`open_view` / `update_view`（入力の画面）、`permalink`、`channel_name`（番号つきの名前。番号を外したテーマの名前は `theme_name(名前)`）、`action_id` / `view_id`、`reply`（`failed=True` なら ⚠️）、`react`、`channel_ids`、`channels(種類)`、`is_owner`、`watch_thread`（メンションなしの返信を拾う）、`claim_thread`（研究テーマのチャンネルでも、そのスレッドの続きを受ける）。失敗の決まった文は `failure_text()`
- 担当: `work(req, folder=, hide=)`（そのチャンネルの作業場を使って担当と会話して答え、答えの本文を返す。研究テーマのチャンネルはテーマのフォルダ、ほかの受け持つチャンネルは `folder`（自分のフォルダ `state_dir` の中）。`hide` の頭で始まる行は Slack に出さない。添付・できたファイル・接続先の許可・引き継ぎ・ジョブは研究と同じ流れ。プロセスの無いモジュールは本体の中で動かす）、`run_ai(用途, 文, folder=, req=, status=, overview=, trigger=)`（AI を1回動かして本文を返す。`folder` を渡さなければ読むだけ、渡せばその中で書ける。`overview` なら研究全体の作業場で読むだけ（研究テーマのフォルダとスレッドの記録を全部読める）。`req` を渡せば経過と答えをスレッドに見せる。動かした時間は Kei Agent の稼働として `trigger` の名前で記録し、上限に当たったら明けるまで定期処理を止める。動かせなければ `AIError`。Slack に出す部分は `final_answer(本文)`）、`ask_agent(skill, 材料)`（`[process]` の担当に仕事を頼む）、`tell_agent(skill, 材料)`（AI を動かさない知らせだけを渡す。届かなくても困りごととして知らせない）、`converse(req)`（担当と会話として答える）、`pick_skill(req)`（言われたことが名刺のどの仕事かを軽いモデルで選ぶ。選べなければ `ASK`）
- 記録: `records`（`put` / `get` / `update` / `items` / `delete`。種類と鍵で1件、中身は JSON にできる辞書。`keep_days` を付けたものは、その日数で毎晩の保守が消す）、`schedule_detail(名前, 日付)`
- 設定: `settings`（module.toml の `[settings]` の既定に、config.toml の `[<名前>]` を重ねた写し）
- 予定カレンダー: `sync_calendar(出典, 予定, day=, days=, complete=, expected_count=)`（出典と ID で照合し、手入力の行には触らない。見えなくなった行は消さずに「要確認」）
- 出来事と App Home: `emit(種類, 中身...)`（出来事を配る）、`home_action_id(名前)`・`home_checkboxes(名前, 値→表示名, 付いている値)`（App Home の項目）、`selected_values(action)`（押された項目の、選ばれた値）
- ほかのモジュール: `ask_module(名前, skill, 材料)`（`[depends]` に書いたモジュールの担当に頼む）
- Daily・振り返り（`core_schedules` を受け持つモジュール）: `morning(now)`（朝の一覧 `Morning`。モジュールの取り込み・予定・朝の一覧の行・うまくいかなかった定期処理を集め、会議は予定カレンダーにも写し、声に予定を渡す。Slack に出せたら `mark_shown(notices)`）、`digest(since, now, 題, agenda=)`（材料。本体の記録・モジュールの材料・研究ホームの Task とノート・前日の振り返り。`DIGEST_CHARS` 字まで）、`gather_prepare(kind, day)`・`gather_agenda(days, kinds)`（モジュールに取り込み直してもらう・予定を集める）、`publish(チャンネル, 見出し, 本文)`（見出しを出し、スレッドに本文。返信はそのチャンネルの担当が続ける）、`collect_conclusions(チャンネル, スレッド, ページ)`（振り返りのスレッドに貼られた結論を日別記録に足す）、`last_ran(名前, before_day=)`（その定期処理が最後に動いた時刻）、`channels("overview")`（本体のチャンネルの名前も読める）
- Toggl: `load_toggl` / `Toggl` / `TogglError` / `TogglAmbiguousWrite`（窓口の kei_agent.api から）
- 実行役: `provider`（App Home で選んだ AI。選ばれていなければ空文字）
- 答えの形: `checked_sections(文, 見出し)`（決まった見出しがこの順で1回ずつあり、どれも中身がある答えだけを通す。見出しの書き方はそろえる）
- 自分のフォルダと Kei Agent 自身: `state_dir`（状態の置き場の `modules/<名前>`。AI に書かせてよいのはこの中だけ）、`repo_root`（Kei Agent のコード）、`check_change(フォルダ, base)`（直した差分を本体の柵で確かめる）、`restart_for_update(前のコミット, 1行)`（作業が終わってから新しい版で起動し直す。起動できなければ本体が前の版に戻す）、`last_update()`（入れ替えたあとの起動なら、その結果 `Update`。`on_start` で読む）、`busy()`（その間は入れ替えを待たせる）、`contains_secret(文)`（公開の場所に書く前に、秘密情報らしいものを見る）
- そのほか: `spawn`（裏で動かす）、`notify_trouble`、`notice_once`、`themes()`（研究テーマの名前・場所・検索キーワード・前提）、`to_thread`（時間のかかる読み書き）。Notion は、Notion のモジュールができるまで `hub`（共通ホーム）と `notion`（研究ホーム）をそのまま渡す

## agent.py の書き方（枠の版 1）

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

## テストの書き方（kei_agent.testing）

`ModuleKit` が、モジュール1つを本番と同じ読み方の設定（一時フォルダの config.toml。頼るモジュールもオンにする）と、偽物の Slack・AI・Notion・担当と一緒に、本体の中で動かす。依頼者として頼み、Kei Agent が出した文を確かめる。

```python
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]      # このテストのあるモジュールのフォルダ（組み込みなら名前でもよい）


async def test_it_answers_the_weather(module_kit):   # pytest の plugin（kei_agent.testing.plugin）の fixture
    kit = module_kit(MODULE, settings={"place": "東京"})   # settings は config.toml の [weather]
    kit.ai.answer("明日は晴れ")                   # AI（FakeAI）が次に返す答え
    ts = await kit.message("明日の天気は？")       # このモジュールのチャンネルで @Kei Agent に頼み、終わるまで待つ
    assert kit.thread(ts) == ["明日は晴れ"]        # そのスレッドに返した文（流して見せた返事も）
    assert kit.ai.calls[0]["use_case"] == "weather_answer"
```

- 依頼者のすること: `message`（`thread=` でスレッドの中）、`reply`（メンションなしの返信）、`invite`（招く。案内が出る）、`slash`、`action`（ボタン）、`view`（入力の画面）、`schedule`（定期処理を1回）、`tick`、`emit`、`home`（App Home）
- 見るもの: `texts()` / `thread(ts)`、`slack`（FakeSlack。呼んだ Slack の API は `calls`）、`ai`（FakeAI）、`records`、`module`（class Module）、`core`、`notion`（FakeNotion）・`hub`（FakeHub）
- 担当プロセス（`[process]`）は、同じプロセスの中で agent.py の Executor を動かす（`LocalAgent`。A2A のサーバーは立てない）。`kit.skill("仕事", {...})` で担当の仕事を直接頼める。`local_agent=False` なら `FakeAgent`（`kit.agent.reply("仕事", "返事")` で並べる）。本物の番地の担当には届かない
- 自分のモジュールのテストは、モジュールのフォルダの `tests/` に置き、`kei-agent module test <名前>` で動かす（`kei-agent module new` のひな形に、そのまま通るテストが入っている）
- テストのあいだ、本物の秘密情報（本体とモジュールの `[secrets]` の名前）を環境変数から外し、利用者のフォルダを一時フォルダにし、本物の状態・launchd・用途の分類器（本物の AI）に触れない。pytest では `-p kei_agent.testing.plugin`（いちばん上の conftest.py なら `pytest_plugins = ["kei_agent.testing.plugin"]`）で、どのテストにも同じ柵が効く。pytest の外では `with ModuleKit(フォルダ, 一時フォルダ) as kit:`
