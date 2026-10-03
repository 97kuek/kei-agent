# 共通の実行基盤

MCP・プロセス・権限・保存の技術リファレンス。使いたいことから読む場合は [使い方](using.md)、機能を足す場合は [モジュールの作り方](modules.md) を参照。

共通コードは MCP クライアントから独立している。以下の図と Dot の説明は、同梱する構成例。[クライアントの役割と拡張方針](extensibility.md#mcp-クライアントと共通コード) を参照。

![プロセスと通信](images/architecture.svg)

## Dot とローカル実行サービス

Dot は対話・クラウド連携・通知配信、Mac は作業場の AI 実行・機械同期・ジョブ・状態保存を受け持つ。大学と知識は Dot の役割で、別のローカル AI は持たない。[用途別の分担](agents.md) と [設計理由](extensibility.md) を参照。

## プロセス

| プロセス | ポート | 起動 | 中身 |
|---|---|---|---|
| 本体 | 8786（MCP は 8785） | `kei-agent` | MCP・知らせの置き場・制限の表・定期処理・状態。Daily・振り返り、時間記録、自己改善もこの中で動く |
| 大学 | 8787 | `kei-agent-module course` | Moodle の機械同期（A2A）。AI は使わない |
| 研究・仕事 | 8788・8789 | `kei-agent-module <名前>` | ローカル AI の担当（[agents.md](agents.md)） |
| トンネル | 8784（点検の画面） | `deploy/run-tunnel.sh` | OpenAI の Secure MCP Tunnel。Mac から OpenAI へ出ていき、Dot の呼び出しを MCP へ渡す |
| 声 | 8790 | `kei-agent-module voice` | 喋る・聞く（下の「声」） |
| Notion ゲートウェイ | 8791 | `kei-agent-module notion` | ローカルの Notion API / MCP（下の「Notion」） |

- どれも launchd で常駐し、`127.0.0.1` だけで話す
- 起動スクリプトは、本体が `deploy/run.sh`、ほかが `deploy/run-agent.sh <名前>`。`uv sync` のあと仮想環境の Python を直に起動する
- 反映は `deploy/update.sh`（[deploy/README.md](../deploy/README.md)）

## 置き場所

```text
~/.config/kei-agent/        自分のもの（KEI_AGENT_HOME で変えられる）
├── agents.csv              モジュールのオンオフ・チャンネル・AI・アカウント
├── schedules.csv           定期処理の時刻とオンオフ
├── config.toml             設定（リポジトリには config.example.toml だけ）
├── profile.md              話し方・所属・興味。会話する担当の指示書に足す
├── prompts/                指示書の差し替え（同じ名前なら、そちらを使う）
├── modules/                自分のモジュール
├── themes.toml             研究テーマとフォルダの対応（任意）
└── secrets/                秘密情報（[paths] secrets で変えられる。AI には読ませない）
~/.local/state/kei-agent/   状態（kei-agent.db、notion.json、asks/、modules/<名前>/）
~/research/<テーマ>/        研究テーマの作業場（agents/research-agent.md）
~/kei-agent/                Kei Agent 自身の作業場（overview/）と、毎晩の書き出し（state/）
```

## 作業場と A2A

Dot が MCP の run に作業場の名前を渡す。作業場から agents.csv と module.toml に従って担当・アカウント・権限を決める。

| 作業場 | 担当 |
|---|---|
| 研究テーマ | 研究 |
| work-<名前> | 仕事（プロジェクトのフォルダ） |
| work | 仕事（現在は会社の Claude） |
| overview | 研究全体 |

- A2A v1.0: 名刺は `/.well-known/agent-card.json`、JSON-RPC の `SendMessage` / `GetTask`、長い仕事は `SendStreamingMessage`
- 合言葉は `KEI_AGENT_A2A_TOKEN`（Bearer）。全プロセスで同じ値
- 担当を呼べるのは本体だけ。声も本体の問い合わせ API（:8786）に頼む
- 名刺の `version` は起動したときの commit。本体は、古い版のまま動く担当を起動し直す
- 返事の形は [agents.md](agents.md#どの担当にも共通)
- 大学の A2A は定型の読み取り・Moodle 同期を受け持ち、AI の `ask` は持たない。授業ホーム・Box の質問と知識の依頼は Dot がクラウドの接続を直接使う

## MCP サーバー

- Dot（Claude Code・Codex からも呼べる）が、Mac の作業場で AI を動かしてもらう MCP サーバー
- AI を動かす道具（`workspaces`・`run`・`status`）と通知の取得（`notices`）は `conversation/hands.py`、読む道具（`agenda`・`reading`・`recent`・`jobs`）の材料は `scheduling/materials.py`、MCP サーバーと認証は `operations/hands_server.py`。読む道具は AI を動かさない（`agenda` だけは、担当が予定を読むのに AI を使うことがある）
- 本体のプロセスの中で `config.toml` の `[hands] url`（127.0.0.1 だけ）に開く。合言葉は秘密情報の `KEI_AGENT_HANDS_TOKEN`（`Authorization: Bearer`）。どちらかが無ければ開かない

| 道具 | 中身 |
|---|---|
| `workspaces` | 頼める作業場（研究テーマ・プロジェクト・担当）と、選べる AI（`agents.csv` の `engines`）・重さ |
| `run` | 作業場・頼みごと・重さ（`light`・`normal`・`deep` → `module.toml` の `[actor] weights` の用途）・AI・会話の番号で AI を動かす。20秒のうちに終われば答え、終わらなければ受付番号。`use_case` で用途を選び、`read_only` で読むだけの実行、`minutes` で許可された上限時間を指定できる |
| `status` | 受付番号の様子と結果（7日残す）。phase は queued・running・done・needs_input・failed、elapsed_seconds は受付からの秒数 |
| `handoff` | 既存の会話を読むだけでまとめ、新しい会話へ要点を保存する。同じ前の会話からは1回だけ作成。長ければ受付番号を返し、中断時は手動で頼み直す |
| `agenda` | これから 1〜14 日の授業・会議・締切（各担当の予定。朝の一覧と同じ材料、件名もそのまま）を時刻順に |
| `reading` | Mac に保存済みの古い記事を取得する互換処理。新しい記事の選定・要約は Dot が行う |
| `save_reading` | 古い `reading` の記事を URL で指定して Notion に保存・解除する互換処理。新しい記事は Dot の Notion 接続で保存する。AI は使わない |
| `recent` | この 1〜168 時間の動き（スレッド・担当ごとの実行と失敗・MCP の頼みごと・終わったジョブ）。本文の抜き出しは研究テーマと研究全体のスレッドだけ |
| `jobs` | 研究のジョブ（動いているものと、2日のうちに終わったもの） |
| `notices` | 未配信の通知を取得する。Slack 投稿に成功した ID を `done` で確認応答するまで残す（`conversation/outbox.py`） |
| `create_workspace` | 研究テーマ・プロジェクトの作業場を作る（既存のフォルダも使える）。研究テーマは研究ホームにも登録する |
| `put_file` / `read_file` | 作業場の `inputs/` に文のファイルを置く／`outputs/` のファイルを読む（Slack の添付の代わり） |
| `timer` | 計測の開始・停止・状態、メモの追加、送信不明の解決。Toggl に記録があると確認した場合は再送せず、無いと確認した場合だけ再送する |
| `sync_submissions` | Moodle の提出・受験終了を確認して Notion の課題に反映する。大学モジュールの `head_action`（AI は使わない） |
| `voice` | 声のスイッチ（知らせる・聞く）。声のモジュールの `head_action` |

- 返す項目: `status`（`done`・`needs_input`・`failed`・`accepted`・`running`）・`text`・`conversation`・`files`（作業場の `outputs/` にできたもの）・`ticket`・`phase`・`elapsed_seconds`
- 担当・アカウント・届く範囲は作業場から決まる。実行時に制限を適用する
- 会話は `mcp` という名前のチャンネルとして記録する（Slack のスレッドとは混ざらない）。研究テーマ・プロジェクトでは、やり取りを作業場の `.kei-agent/threads/<会話の番号>.md` にも残す
- 会話番号は作業場に結び付く。AI・指示書の変更やセッションの消失時は、保存した依頼と回答から会話を復元する
- Slack 通知の送信元は Dot だけ。notices の done=[] で取得し、Slack に送信できた id だけ確認済みにする
- ChatGPT（Dots）からは、OpenAI の Secure MCP Tunnel を通して届く。トンネルのプログラム（`tunnel-client`。`deploy/run-tunnel.sh` が launchd で動かす）がこの Mac から OpenAI へ出ていき、届いた呼び出しに合言葉を付けて MCP へ渡す。番号は `[hands] tunnel`、鍵はトンネルだけの `kei-agent-tunnel.zsh`。MCP は OAuth を使わず、`/.well-known/` には本文の無い 404 を返す
- MCP の道具を変更した場合の Dot 側での再スキャンは [接続手順](dots.md) を参照

## AI の動かし方

- AI を起動するのは `runner.run_model` だけ（Claude も Codex も）
- 1回の条件は `ExecutionRequest` と `ExecutionContract` にまとめる
- 触れる範囲は制限の表（`agent_policy.py`）が決める。担当ごとの表は [agents.md](agents.md#触れる範囲制限の表)

| | Claude | Codex |
|---|---|---|
| 起動 | `claude -p`（stream-json） | `codex exec --json` |
| 許可 | 表から `--settings` の許可と拒否を作り、`dontAsk` | 表から一時的な権限 profile `kei_agent_scoped` を作る |
| ユーザー設定 | 持ち込まない（`--setting-sources ""`、`--strict-mcp-config`） | 持ち込まない（`--ignore-user-config`） |
| 連携 | その担当のアカウント（`agents.csv` の `claude_account`）の、表に書いた道具だけ | 表の App の、表に書いた読む道具だけ |

- 道具は線から決まる。自分のデータを読む担当（`[actor] data = "own"`。既定）は、作業場の読み書き・コマンド・手分け（サブエージェント）・Web・コマンドの通信（どこへでも）を使える。会社のデータを読む担当（`data = "company"`。仕事）は、作業場の読み書きとコマンドだけで、外へ出られない
- read-only（読むだけ）の実行は、書く・動かす・通信する手段を外す（声からの問い合わせ、分類など）
- plugin は担当の1つと、共通のもの（リポジトリの `plugins/`）だけ。Notion を使える担当には `plugins/notion`（既存のページの書式を保つ skill）を足す。フック（`plugin/hooks/policy.py`）は担当の plugin のもので、安全違反を断る第二の防御として Claude にも Codex にも掛ける
- Notion の MCP は `kei-notion`。合言葉は担当の名前で作る
- 作業場の前提は `AGENTS.md` に置く（Codex が読む）。`CLAUDE.md` はそれを読み込む1行（Claude Code が読む）。Kei Agent が作った作業場の古い `CLAUDE.md` は、使うときに `AGENTS.md` へ移す。既存のフォルダの `CLAUDE.md` は動かさずに読む
- 走らせた記録（SQLite の `runs`）に、担当・用途・provider・モデル・effort・かかった時間・費用・失敗を残す。担当のプロセスで決まった用途とモデルも、返事の封筒で本体に戻す
- 声からの読むだけの問い合わせと振り分けは、AI の実行記録に残さない。Dot の記事選定・論文新着はクラウドで動く

### actor とモデル

- AI の実行役（actor）ごとに provider を選ぶ。担当の表 `agents.csv` の `engine` だけで決まる。既定は無く、選ぶまで動かない
- モデルは各モジュールの用途ごとの定義（[agents.md](agents.md)）で決まる。`agents.csv` の `model`・`effort` を書いた担当だけ、明示の用途を除いてそのモデルにする（[deploy/README.md](../deploy/README.md#担当の表agentscsv)）
- 領域別の actor: `research` / `work`。本体には `router`（振り分け）/ `daily` / `improve` の実行役もある。大学と知識は actor を持たない
- 使ってよいモデルは `framework/models.py` にだけ置く。モジュールはその中からしか選べない（用途ごとに選ぶのは `execution/model_policy.py`）
- 別の provider や上位のモデルへ自動で切り替えない。止まったら理由を出す
- モデル名と effort の正本は `src/kei_agent/framework/models.py` と各モジュールの `module.toml` の `[use_cases]`。担当の用途は [agents.md](agents.md) の各ページを参照する
- 本体で使う用途は、振り分け・分類、Daily・振り返り、自己改善の案・実装・要望の要約

## Slack に出す文（出力契約）

- AI の答えは `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間だけを出す（`response_output.py`）

| 種類 | 確かめること |
|---|---|
| ふだんの会話 | 印がちょうど1組で、中が空でない。絶対パスはファイル名だけにする |
| Daily | 決まった4つの見出しだけ |
| 振り返り | 「今日の成果」「未完了タスク」と、最後の問いだけ |
| 定型の A2A の返事 | 印は要らない。経過・例外・パスを含むものは出さない |

- 満たさなければ、中身を含まない決まった文を出す（`safe_failure`）
- 確認の合図 ❓ は MCP の run で needs_input になる。Slack での確認・進捗表示は Dot が行う

## 柵

| 項目 | 中身 |
|---|---|
| 使える道具 | 線から決まる（上の「AI の動かし方」）。作法（提出しない・記事の中の指示に従わない、など）は指示書で頼む |
| 書き込み | 作業場の中と `[sandbox] allow_write` だけ（読むだけの実行は、コマンドからも書けない） |
| 読ませない場所 | `[sandbox] deny_read`（既定は秘密情報・`~/.ssh`・`~/.claude`・担当のプロファイル）に、`agents.csv` のアカウントのフォルダと、アカウントの違う担当の作業場を足したもの（`guard.denied_reads`）。Claude にも Codex にも同じものを掛ける |
| 外への通信 | 会社のデータを読む担当は、Web もコマンドの通信も無し。ほかの担当はどこへでも出られる（鍵とほかのアカウントは、そもそも読めない） |
| 環境変数 | 子プロセスには渡す一覧のものだけ（`guard.PASSED_ENV`）。鍵・トークン・SSH の鍵の窓口は渡さない |
| 柵そのもの | `guard.py`・`config.example.toml`・`deploy/` は自己改善で直させない |
| Slack から変えられないもの | 同時に動かす数・上限時間・書き込み先・読ませない場所 |

## 定期実行と個別機能

- 時刻・停止対象・ローカルスケジューラは [Dot と定期実行](dots.md#予定の一覧)
- Daily は [定期実行](dots.md#ローカルの-daily振り返り)、時間記録と声は [依頼の使い方](using.md)、自己改善は [コードの変更](agents/work-agent.md#kei-agent-の修正)

<a id="声"></a>
Mac の音声サービスの仕様は [Mac の声](using.md#mac-の声) を参照。

## Notion

以下は Mac の Notion ゲートウェイ。Dot 自身の Notion プラグインとは認証・権限が別で、Dot の操作はこのゲートウェイを通らない。

- 鍵 `NOTION_TOKEN` を持つのはゲートウェイ（:8791）だけ。ほかの起動スクリプトは読んだあとで消す
- 合言葉は使う側（client）ごと。親の合言葉 `KEI_AGENT_NOTION_GATEWAY_TOKEN` から HMAC-SHA256 で作る
- 届くホームは `agents.csv` の `notion` 列（共通ホームは overview の行）

| client | 使うところ | 届くホーム | API / MCP |
|---|---|---|---|
| `kei-agent` | 本体、setup などの CLI | 共通と、書いてあるすべてのホーム | `/mcp`、`/notion/v1` |
| `course` | Moodle の機械同期 | 授業ホーム | `/mcp`、`/notion/v1` |
| `research` | 研究の AI | 研究ホーム | `/mcp` だけ（コマンドを使えるので） |
| そのほかのモジュール | そのモジュール | `agents.csv` のその行の `notion` のホーム | `/mcp`（シェルを使う AI がいなければ `/notion/v1` も） |

- 要求ごとに、触れる ID がホームの子孫かを親をたどって確かめる。外なら 403
- `/mcp` の道具は12個: `read` `search` `query` `create_page` `update_page` `append_blocks` `replace_content` `update_block` `delete_block` `create_database` `update_data_source` `move`
- 記録に残すのは時刻・client・操作・対象の ID・成否だけ。本文は残さない
- DB と項目は名前で読む。Notion の画面で名前を変えるなら、`notion.py`・`notion_store.py`・`notion_hub.py`・`notion_hub_setup.py`・`modules/course/notion_setup.py` も直す

### 共通ホーム

| ページ・DB | 中身 |
|---|---|
| 日別記録 | 1日1行。Daily・レトプラ・それぞれの Slack |
| 予定カレンダー | 出典（Outlook / Google Calendar / 課題 / 手入力）つきの予定。見えなくなった行は消さず「要確認」 |
| 時間記録 | 研究・大学・仕事の時間。週ごとのグラフのビュー |
| 今週のタスク | 締切が今週・来週の研究 Task と課題、期限切れで終わっていないもの |
| 読みもの | 保存を選んだ記事（気になる / 読んだ） |
| 学びのノート | 振り返りの会話で言語化した学び・助言（題・日付・分野・種類・出典。本文は場面・学んだこと・次にどう使うか） |
| 収集 | Dot が毎朝読む興味と情報源（[knowledge-agent.md](agents/knowledge-agent.md)） |

- 作るのは `uv run kei-agent-hub-setup --apply`
- 研究ホームは [research-agent.md](agents/research-agent.md)、授業ホームは [course-agent.md](agents/course-agent.md)

## コードの地図

`src/kei_agent/` は領域ごとのフォルダに分ける。モジュールが触れるのは、直下の窓口 `api.py`（`Core`）だけ。

- 依存の向きは一方向: モジュールの枠・設定 ← 記録 ← 作業場 ← AI の実行 ← 会話 ← 予定 ← 窓口 ← 運用
- 下の層から上の層は読み込まない（型の注釈だけは別）。破ると `tests/test_layers.py` が落ちる
- 例外は1つ: 会話の本体（`assistant.py`）は、モジュールを迎え入れるために窓口の `Core` を作る

| 領域 | 中身 |
|---|---|
| `configuration/` | `config.toml`・担当の表 `agents.csv`（`agents_table.py`）・既存のフォルダの対応 `themes.toml` とチャンネル名の決まり（`places.py`）・起動スクリプトが知りたい場所 |
| `storage/` | SQLite（`store.py`・`records.py`）、定期処理の見出しと時刻・担当ごとの AI を設定から引く `settings.py`と Notion（`notion.py` の土台・研究ホーム `notion_store.py`・共通ホーム `notion_hub.py`（作るのは `notion_hub_setup.py`）） |
| `framework/` | モジュールの定義の読み込み（`modules.py`）、使ってよいモデルの一覧とその確かめ（`models.py`）、動いている版 |
| `workspaces/` | チャンネルから作業場を決める（`themes.py`。研究テーマ・プロジェクト・モジュール）、作業場のファイル |
| `execution/` | AI の起動（`runner.py`）、制限の表、用途ごとのモデル、実行の条件、権限、柵（`guard.py`）、用途の分類、担当との通信（A2A）、ジョブ、新しい版での起動し直し |
| `scheduling/` | 定期実行（`schedule.py`）、朝の一覧、材料集め（Dot に渡す材料 `materials.py` も）、締切、毎晩の保守、予定カレンダー、時間 |
| `conversation/` | 依頼から返事までの実行サービス（`assistant.py`、生成は `service.py`）、MCP 実行（`hands.py`）、通知（`outbox.py`）、保存した会話、振り分け、出力契約、引き継ぎ、日付の言い方 |
| `operations/` | 起動（`app.py`）、MCP サーバー（`hands_server.py`）と `kei-agent` のコマンド（setup・doctor・module・agents）、モジュールのひな形、取り込みの確かめ |
| `testing/` | モジュールのテストの道具（[modules.md](modules.md#テストの書き方kei_agenttesting)） |
