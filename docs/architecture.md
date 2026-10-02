# 仕組み

- Kei Agent のいまの作り（本体と、モジュールの枠）
- 担当ごとの中身は [`agents.md`](agents.md)、モジュールの作り方は [`modules.md`](modules.md)

![プロセスと通信](images/architecture.svg)

## プロセス

| プロセス | 番地 | 起動 | 中身 |
|---|---|---|---|
| 本体 | 8786 | `kei-agent` | Slack の受け口・振り分け・制限の表・定期処理・App Home。Daily・振り返り、時間記録、自己改善もこの中で動く |
| 大学・研究・仕事・知識 | 8787・8788・8789・8792 | `kei-agent-module <名前>` | 担当（[agents.md](agents.md)） |
| 声 | 8790 | `kei-agent-module voice` | 喋る・聞く（下の「声」） |
| Notion ゲートウェイ | 8791 | `kei-agent-module notion` | Notion への唯一の口（下の「Notion」） |

- どれも launchd で常駐し、`127.0.0.1` だけで話す
- 起動スクリプトは、本体が `deploy/run.sh`、ほかが `deploy/run-agent.sh <名前>`。`uv sync` のあと仮想環境の Python を直に起動する
- 反映は `deploy/update.sh`（[deploy/README.md](../deploy/README.md)）

## 置き場所

```text
~/.config/kei-agent/        自分のもの（KEI_AGENT_HOME で変えられる）
├── config.toml             設定（リポジトリには config.example.toml だけ）
├── profile.md              話し方・所属・興味。会話する担当の指示書に足す
├── prompts/                指示書の差し替え（同じ名前なら、そちらを使う）
├── modules/                自分のモジュール
├── themes.toml             研究テーマとフォルダの対応（任意）
└── secrets/                秘密情報（[paths] secrets で変えられる。AI には読ませない）
~/.local/state/kei-agent/   状態（kei-agent.db、notion.json、asks/、modules/<名前>/）
~/research/<テーマ>/        研究テーマの作業場（agents/research-agent.md）
~/course/                   大学の作業場
~/kei-agent/                Kei Agent 自身の作業場（overview/）と、毎晩の書き出し（state/）
```

## Slack の受け口

| 項目 | 中身 |
|---|---|
| つなぎ方 | Socket Mode。頼めるのは `KEI_AGENT_ALLOWED_USER_ID` の1人だけ |
| チャンネル | 頭の番号を外して `agents.csv` の `channels` と照合する。当たらなければ研究テーマ |
| 返事 | `chat.startStream` で流す。経過は `assistant.threads.setStatus`、状態は `agents.sessions.setStatus` |
| 並行 | 違うスレッドは `max_concurrent_runs`（既定2）まで。同じスレッドは順番 |
| 会話 | 1スレッド = 1会話（[agents.md](agents.md#どの担当にも共通)） |
| 日付 | どの担当への依頼にも、頭に今日の日付と曜日を付ける |
| 再起動 | 処理中の依頼は控えを残し、止まったものは起動してからやり直す |
| 上限 | 明ける時刻をスレッドに書き、明けたらやり直す（分からなければ30分後）。明ける前に書かれたら ⏳ を付けて、やり直しの予約をその依頼に置き換える（様子を聞かれたら、明ける時刻を答える） |
| 様子の確認 | 短い確認（`slack_text.is_status_inquiry`）は、作業中ならその場で答え、そうでなければ読むだけで動かす |
| 返事待ち | ❓ の確認・失敗したジョブのあとだけ。エラーや上限で止まった回は返事待ちにしない（`stalled_request` に残し、次の返信に文脈として渡す） |
| 知らせ | スレッドの投稿はメンションが無いと通知が届かない。返事待ちになったとき、60秒以上・ジョブの報告・やり直しの回が終わった・止まったときに、依頼者へのメンションを足す |
| 外の文字 | 予定の件名などは、出す前に `<` `>` `&` を逃がす |

## 振り分けと A2A

| チャンネル | 行き先 |
|---|---|
| 研究テーマ | 研究 |
| `#work-<名前>`（頭が一致するチャンネル。`[channels]` に `"work-*"`） | 仕事。作業場は担当のフォルダ（`agents.csv` の work の行の `folder`）の下の `<名前>` |
| `#2-course`・`#3-work`・`#4-knowledge` など | そのモジュールの `on_message`。担当のスキルか `ask` に頼む |
| `#0-overview` | 軽いモデルが、名刺のスキル一覧から相手と仕事を選ぶ |
| `#0-kei-agent` | 自己改善。オフなら、困りごとを知らせるだけの場所 |

- A2A v1.0: 名刺は `/.well-known/agent-card.json`、JSON-RPC の `SendMessage` / `GetTask`、長い仕事は `SendStreamingMessage`
- 合言葉は `KEI_AGENT_A2A_TOKEN`（Bearer）。全プロセスで同じ値
- 担当を呼べるのは本体だけ。声も本体の口（:8786）に頼む
- 名刺の `version` は起動したときの commit。本体は、古い版のまま動く担当を起動し直す
- 返事の形は [agents.md](agents.md#どの担当にも共通)

## 手の口（MCP）

- 頭（OpenAI Dots。今は Claude Code・Codex）から、作業場で AI を動かしてもらう入口。Slack の受け口と並ぶもう1つの入口（計画は [#17](https://github.com/97kuek/kei-agent/issues/17)）
- AI を動かす道具（`workspaces`・`run`・`status`）と投稿の道具（`post`）は `conversation/hands.py`、読む道具（`agenda`・`reading`・`recent`・`jobs`）の材料は `scheduling/materials.py`、口そのもの（MCP・合言葉）は `operations/hands_server.py`。読む道具は AI を動かさない（`agenda` だけは、担当が予定を読むのに AI を使うことがある）
- 本体のプロセスの中で `config.toml` の `[hands] url`（127.0.0.1 だけ）に開く。合言葉は秘密情報の `KEI_AGENT_HANDS_TOKEN`（`Authorization: Bearer`）。どちらかが無ければ開かない

| 道具 | 中身 |
|---|---|
| `workspaces` | 頼める作業場（研究テーマ・プロジェクト・担当）と、選べる AI（`agents.csv` の `engines`）・重さ |
| `run` | 作業場・頼みごと・重さ（`light`・`normal`・`deep` → `module.toml` の `[actor] weights` の用途）・AI・会話の番号で AI を動かす。20秒のうちに終われば答え、終わらなければ受付番号 |
| `status` | 受付番号の様子と結果（7日残す） |
| `agenda` | これから 1〜14 日の授業・会議・締切（各担当の予定。朝の一覧と同じ材料、件名もそのまま）を時刻順に |
| `reading` | 知識の担当がこの 1〜7 日に出した読みもの（👍 したか、保存したか）。モジュールの `head_materials` |
| `recent` | この 1〜168 時間の動き（スレッド・担当ごとの実行と失敗・手の口の頼みごと・終わったジョブ）。本文の抜き出しは研究テーマと研究全体のスレッドだけ |
| `jobs` | 研究のジョブ（動いているものと、2日のうちに終わったもの） |
| `post` | 研究全体のチャンネル（`overview`）に Kei Agent の名前で投稿する。`text` はチャンネルに、`details` はそのスレッドに。ほかのチャンネルには出せない。返信は研究全体のスレッドとして受ける |

- 返す項目: `status`（`done`・`needs_input`・`failed`・`accepted`・`running`）・`text`・`conversation`・`files`（作業場の `outputs/` にできたもの）・`ticket`
- 担当・アカウント・届く範囲は作業場から決まる。線は Slack から頼んだときと同じ実行の仕組みが守る
- 会話は `mcp` という名前のチャンネルとして記録する（Slack のスレッドとは混ざらない）
- ChatGPT（Dots）からは、OpenAI の Secure MCP Tunnel を通して届く。トンネルのプログラム（`tunnel-client`。`deploy/run-tunnel.sh` が launchd で動かす）がこの Mac から OpenAI へ出ていき、届いた呼び出しに合言葉を付けて手の口へ渡す。番号は `[hands] tunnel`、鍵はトンネルだけの `kei-agent-tunnel.zsh`。手の口は OAuth を使わず、`/.well-known/` には本文の無い 404 を返す
- ChatGPT の MCP のアプリは、作ったときの道具の一覧を使い続ける（あとでサーバーが道具を増やしても、読み直しに来るだけで見える一覧は変わらない）。道具を足したり変えたりしたら、ChatGPT で MCP を作り直す

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
- 残さないのは、知識の朝の読みもの・論文の新着、声からの問い合わせ、振り分け

### actor とモデル

- AI の実行役（actor）ごとに provider を選ぶ。担当の表 `agents.csv` の `engine` だけで決まる（App Home は見せるだけ）。既定は無く、選ぶまで動かない
- モデルは用途ごとの表（下と [agents/](agents.md)）で決まる。`agents.csv` の `model`・`effort` を書いた担当だけ、明示の用途を除いてそのモデルにする（[deploy/README.md](../deploy/README.md#担当の表agentscsv)）
- actor: `research` / `course` / `work` / `knowledge` / `router`（振り分け）/ `daily` / `improve`
- 使ってよいモデルは `framework/models.py` にだけ置く。モジュールはその中からしか選べない（用途ごとに選ぶのは `execution/model_policy.py`）

| provider | 使ってよいモデル |
|---|---|
| Claude | `claude-haiku-4-5` / `claude-sonnet-5` / `claude-opus-5` / `claude-fable-5` |
| Codex | `gpt-6-luna` / `gpt-6-sol` / `gpt-6-astra` |

- 別の provider や上のモデルへ自動で切り替えない。止まったら理由を出す
- 担当の用途とモデルは [agents/](agents.md) の各ページ、本体の用途は次の表

| 用途 | Claude | Codex |
|---|---|---|
| 振り分け・分類 | haiku-4-5 | luna / low |
| Daily / 振り返り | sonnet-5 / medium・opus-5 / high | luna / medium・sol / high |
| 自己改善: 案 / 実装 / issue の要約 | opus-5 / high・sonnet-5 / high・haiku-4-5 | sol / xhigh・sol / high・luna / low |

## Slack に出す文（出力契約）

- AI の答えは `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間だけを出す（`response_output.py`）

| 種類 | 確かめること |
|---|---|
| ふだんの会話 | 印がちょうど1組で、中が空でない。絶対パスはファイル名だけにする |
| Daily | 決まった4つの見出しだけ |
| 振り返り | 「今日の成果」「未完了タスク」と、最後の問いだけ |
| 定型の A2A の返事 | 印は要らない。経過・例外・パスを含むものは出さない |

- 満たさなければ、中身を含まない決まった文を出す（`safe_failure`）
- 最後の合図（❓ 🧵 🛠 📦 ✅ 🗑）は印の中の末尾に書き、本体がボタンや状態にする（[using.md](using.md#返事の最後の合図)）

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

## 定期実行

- 本体のスケジューラが毎分動く（`schedule.py`）
- 時刻とオンオフは定期処理の表 `schedules.csv`（書いていない処理は既定の時刻）

| 名前 | 既定 | 受け持ち |
|---|---|---|
| `literature` / `reading` | 07:00 | 知識 |
| `intake` | 08:00 | 本体（朝の取り込み。Daily が止まっているときだけ動く。投稿せずに、モジュールの取り込み直しと、声への1週間の予定。会議を予定カレンダーへ写すのは Dot） |
| `daily` | 08:00 | Daily・振り返り |
| `review` | 21:00 | Daily・振り返り |
| `toggl_import` | 22:00 | 時間記録 |
| `maintenance` | 22:00 | 本体（古いファイルの整理とバックアップ） |
| `night` | 00:00 | 本体（🌙 の Task） |

- 同じ時刻なら、夜間の Task → モジュールの定期処理 → 朝の取り込み → Daily → 振り返り → 保守の順
- Dot の予定に移した処理と、そのとき止めるものは [dots.md](dots.md)
- スリープで逃した処理は3時間以内なら動かす（`night` は12時間以内）。上限中は明けてから
- 毎分、24時間放っておかれた返事待ち（❓ の確認・失敗したジョブのあと）に一度だけ声をかける
- 1回だけ動かす: `uv run kei-agent-schedule <名前>`（`--record` を付けなければ記録に残らない）

## 本体の中のモジュール

### Daily・振り返り（`daily`）

- 朝の一覧（`core.morning`）を出し、そのスレッドに Daily を書く
- 材料（`core.digest`）は本体の記録・モジュールの材料・研究ホーム・前日の振り返り。3万字を超えたら後ろから削る
- 研究全体の作業場を読むだけで動かす（`core.run_ai(overview=True)`）
- 答えの見出しが違えば出さず、決まった文で知らせる（`core.checked_sections`）
- 共通ホームの「日別記録」に1日1行で残す。振り返りのスレッドは daily が引き取り（`core.claim_thread`）、学びの会話を「学びのノート」に残して、題とリンクを日別記録にも足す

### 時間記録（`time`）

- 測れるのは1本。止めたら Toggl、次に共通ホームの「時間記録」に送る
- 送れなければ保留にし、毎分の見回り（`tick`）で送り直す
- `toggl_import` は、Toggl のアプリで直接測った直近7日ぶんを取り込む（プロジェクト名が `研究/` `大学/` `仕事/` で始まるものだけ）
- 設定は `[time] prefixes`（測るチャンネル）と `pick_course`（科目を選ぶチャンネル）

### 自己改善（`improve`）

| 段 | 中身 |
|---|---|
| 要望 | 軽い用途が題と本文に要約し、`gh` で公開の issue（ラベル `kei-agent-request`）にする。URL・パス・秘密情報などを含む要約は捨てる |
| 案 | リポジトリを読むだけで考える。書けるのは相談用のフォルダだけ |
| `🛠 着手` | `<state_dir>/modules/improve/worktrees/` の worktree で直す。書けるのは worktree の中だけ |
| `📦 取り込み` | 柵のファイルに触れた差分は捨てる。テスト・ruff・鍵・大きなファイルを確かめ、早送りで取り込んで push する |
| 起動し直し | 作業が無くなったら新しい版で起動し直す。3回つながらなければ `deploy/run.sh` が `git revert` して前の版に戻す。起動できたら issue を閉じる |
| `✅ 解決済み` / `🗑 見送り` | 直さずに要望を終わりにし、issue を閉じる（見送りは not planned）。取り込み待ちの直しは捨てる |

- 合図は、その回が依頼者の投稿で始まったときだけ効く（`✅` と `🗑` は依頼者が一度答えてから）。同時に直すのは1つだけ

## 声

| 役目 | 中身 |
|---|---|
| 聞く・喋る | OpenAI Realtime API（`gpt-realtime-2.1-mini`）。鍵が無ければつながらないが落ちない |
| 音 | Mac の `ffmpeg` |
| 予定・様子 | 本体が朝に渡した1週間ぶんのデータ |
| 研究・授業・仕事の中身 | 本体の問い合わせ口（:8786）に読むだけで聞く |
| 作業の依頼 | `<state_dir>/asks/` にファイルを置き、本体がスレッドを立てる |
| 顔 | `KEI_AGENT_STACKCHAN_URL` があれば Stack-chan に表情を送る |

- 本体とモジュールは出来事を配る（`core.emit`）。声は `on_event` で受け取り、担当プロセスに渡す
- 渡す出来事: `schedule` `due` `working` `done` `failed` `limited` `awaiting` `listen`
- 会話は60分で切れるのでつなぎ直す。話した中身は残さない

## Notion

- 鍵 `NOTION_TOKEN` を持つのはゲートウェイ（:8791）だけ。ほかの起動スクリプトは読んだあとで消す
- 合言葉は使う側（client）ごと。親の合言葉 `KEI_AGENT_NOTION_GATEWAY_TOKEN` から HMAC-SHA256 で作る
- 届くホームは `agents.csv` の `notion` 列（共通ホームは overview の行）

| client | 使うところ | 届くホーム | 口 |
|---|---|---|---|
| `kei-agent` | 本体、setup などの CLI | 共通と、書いてあるすべてのホーム | `/mcp`、`/notion/v1` |
| `course` | 大学の決まった処理と AI | 授業ホーム | `/mcp`、`/notion/v1` |
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
| 予定カレンダー | 出典（Outlook / 課題 / 手入力）つきの予定。見えなくなった行は消さず「要確認」 |
| 時間記録 | 研究・大学・仕事の時間。週ごとのグラフのビュー |
| 今週のタスク | 締切が今週・来週の研究 Task と課題、期限切れで終わっていないもの |
| 読みもの | 👍 した記事（気になる / 読んだ） |
| 学びのノート | 振り返りの会話で言語化した学び・助言（題・日付・分野・種類・出典。本文は場面・学んだこと・次にどう使うか） |
| 収集 | 知識が毎朝読む興味と情報源（[knowledge-agent.md](agents/knowledge-agent.md)） |

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
| `execution/` | AI の起動口（`runner.py`）、制限の表、用途ごとのモデル、実行の条件、権限、柵（`guard.py`）、用途の分類、担当に頼む口（A2A）、ジョブ、新しい版での起動し直し |
| `scheduling/` | 定期実行（`schedule.py`）、朝の一覧、材料集め（頭に渡す材料 `materials.py` も）、締切、毎晩の保守、予定カレンダー、時間 |
| `conversation/` | Slack の依頼から返事までの本筋（`assistant.py` と、役割ごとに混ぜる部品）、手の口で AI を動かす（`hands.py`）、振り分け、出力契約、引き継ぎ、App Home、置き場所の選び方、日付の言い方 |
| `operations/` | 起動（`app.py`）、手の口の MCP（`hands_server.py`）と `kei-agent` のコマンド（setup・doctor・manifest・module・agents）、モジュールのひな形、取り込みの確かめ |
| `testing/` | モジュールのテストの道具（[modules.md](modules.md#テストの書き方kei_agenttesting)） |
