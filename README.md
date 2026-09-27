# Kei Agent

> Slack で頼んだ用事を、担当のエージェントが進めるパーソナルアシスタント。機能はどれもモジュールで、自分のモジュールを足して作り替えられる

## 何ができるか

組み込みのモジュール（`modules/`）。使うものだけをオンにする（`kei-agent module add / remove`。設定の `modules`）。

| モジュール | 名前 | できること |
|---|---|---|
| 研究 | `research` | テーマのチャンネル（`#10_<テーマ>`）ごとの作業場（`~/research/<テーマ>/`）でコードを書き、実験を回し、論文を探して、研究ホーム（Notion）に残す。長い処理はジョブにする |
| 大学 | `course` | Moodle の締切を知らせ、Box の学部要項・過去問と、Notion の授業ホーム（課題・成績・単位）を読んで答える |
| 仕事 | `work` | 会社の Outlook・メール・Teams・SharePoint を読んで答える（送信や予定の変更はしない） |
| 知識 | `knowledge` | 興味のある技術記事と、研究テーマの論文の新着を毎朝選んで要約し、その質問に答える |
| Daily・振り返り | `daily` | 朝に今日の予定と Daily、夜に振り返り（今日の成果と未完了）を出し、共通ホームの日別記録に残す |
| 時間記録 | `time` | `/toggl` と固定したカードで研究・大学・仕事の時間を測り、Toggl と Notion の「時間記録」に送る |
| 自己改善 | `improve` | `#00_kei-agent` で要望を聞き、Kei Agent 自身のコードを直して、承認されたものだけをテストして取り込む |
| 声 | `voice` | 出来事（依頼が終わった、締切が近い）を喋って知らせ、App Home で「聞く」を入れるとマイクで会話する |
| Notion | `notion` | Notion に届く唯一の口（ゲートウェイ）。使う側ごとに、届くホームを絞る |

どれも任意で、要るのは Slack と、AI（Claude Code か Codex の CLI）とコアだけ。定期処理の時刻は App Home と `config.toml` の `[schedule]` で変えられる。

## 仕組み

コアは枠だけ（Slack の受け口と振り分け、AI を動かす仕組みと制限の表、保存、定期処理、App Home、常駐）で、機能はモジュールが持つ。

```text
Slack（自分用のワークスペース、Socket Mode）
  │
  ▼
Kei Agent 本体（src/kei_agent/）          :8786  声からの問い合わせ口
  Slack の受け口・振り分け・制限の表・定期処理・App Home
  本体の中で動くモジュール: Daily・振り返り、時間記録、自己改善
  │  A2A（127.0.0.1、共有の合言葉）。担当を呼べるのは本体だけ
  ├─ 大学    :8787  Moodle / Box / 授業ホーム
  ├─ 研究    :8788  テーマの作業場で AI を動かす / pueue のジョブ
  ├─ 仕事    :8789  Microsoft 365（読むだけ）
  ├─ 声      :8790  知らせを喋る / マイクで会話
  ├─ 知識    :8792  読みもの・論文の新着
  └─ Notion  :8791  Notion に届く唯一の口（ゲートウェイ）
```

担当はそれぞれ Claude Code CLI か Codex CLI で動く（App Home で担当ごとに選ぶ）。どちらでも、使える道具と届く範囲は、モジュールの宣言から作る制限の表で決まる。すべて同じ Mac の launchd で常駐する。

## はじめる

要るもの: macOS、自分用の Slack のワークスペース、Claude Code か Codex の CLI（ログイン済み）、[uv](https://docs.astral.sh/uv/)、pueue（研究のジョブ）。あれば使うもの: Notion、Toggl、OpenAI（声の会話）。

```zsh
git clone https://github.com/97kuek/kei-agent.git ~/src/kei-agent
cd ~/src/kei-agent
brew install pueue ffmpeg && brew services start pueue
uv sync --all-groups
uv run kei-agent setup     # 質問に答えると、設定・プロフィール・秘密情報のファイルを作り、常駐の登録まで進む
uv run kei-agent doctor    # 困ったら点検（読むだけ）
```

自分の設定は、リポジトリの外の `~/.config/kei-agent/` に置く（`config.example.toml` と `profile.example.md` を写して書き換えてもよい）。Slack App・秘密情報・Notion・launchd の詳しい手順は [`deploy/README.md`](deploy/README.md)。

## 自分のモジュールを作る

```zsh
uv run kei-agent module new weather --ai    # ひな形（~/.config/kei-agent/modules/weather/）。担当プロセスも持つなら --process
uv run kei-agent module test weather        # 本物の Slack・AI・秘密情報に触れないテスト
uv run kei-agent module add weather         # オンにする（そのあとにやることを並べる）
```

書き方は [`docs/modules.md`](docs/modules.md)。

## ドキュメント

| 読みたいこと | 場所 |
|---|---|
| Slack での使い方（チャンネル、合図、時間記録、App Home、声） | [`docs/using.md`](docs/using.md) |
| いまの仕組み（プロセス、provider とモデル、出力、柵、Notion、定期実行、声） | [`docs/architecture.md`](docs/architecture.md) |
| 入れ方と運用（Slack App、秘密情報、launchd、Notion の準備、困ったとき） | [`deploy/README.md`](deploy/README.md) |
| 自分のモジュールを作る（作る → 試す → オンにする、module.toml・module.py・agent.py・テストの書き方） | [`docs/modules.md`](docs/modules.md) |
| 自分用に作り替える設計（コアとモジュール、`~/.config/kei-agent/`、セットアップ、移行の段階） | [`docs/extensibility.md`](docs/extensibility.md) |
| 開発の手順、テスト、書き方 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## ライセンス

MIT（`LICENSE`）
