# Kei Agent

Kei Agent は、対話からローカルの作業実行までをつなぐ、モジュール式のアシスタント。使う機能・AI・アカウントを設定し、自分の用途に合わせて拡張できます。

- MCP クライアントから、作業の依頼・進捗確認・通知の取得ができます。Dot は任意のクライアントです
- 共通コードが実行管理・権限・状態保存を受け持ち、領域ごとの機能はモジュールとして追加します
- 個人のプロフィール・接続情報・モジュールの選択はリポジトリの外に置きます
- Slack・通話 → Dot → MCP → Mac の構成と、コピーして使える Dot 用プロンプトを同梱しています。通話の Slack 記録は利用者の依頼と投稿権限に従います

下の図と表のクラウド連携は、Dot を使う構成例です。共通部分とクライアントの役割は [設計の決めごと](docs/extensibility.md) を参照してください。

![Kei Agent の全体図](docs/images/overview.svg)

## 何ができるか

| モジュール | 名前 | できること |
|---|---|---|
| 研究 | `research` | テーマのチャンネルごとの作業場で、コード・実験・論文探しをし、研究ホーム（Notion）に残す |
| 大学 | `course` | AI を使わず、起動・復帰時と30分ごとに Moodle の締切を取り込み、API 設定時は10分ごとに提出・受験終了を Notion に同期する。授業ホーム・Box の質問は Dot が直接答える |
| 仕事 | `work` | 会社の Claude Code で Teams・SharePoint を読んで答える。プロジェクトの作業場でコードを書いてテストを動かす。Outlook は Dot が直接扱う |
| 知識 | `knowledge` | Mac に残るのは保存済みの古い記事を `save_reading` で保存・解除する互換処理。検索・記事要約・Notion への保存・毎朝の選定は Dot が直接行う |
| Daily・振り返り | `daily` | 朝に今日の予定と Daily、夜に振り返りを出す（Dot の予定を使う構成では、このモジュールの定期処理を止める） |
| 夜間の Task | `night` | Notion の 🌙 Task を研究担当に渡し、結果と確認待ちを記録する（必要な構成だけでオンにする） |
| 時間記録 | `time` | 時間を測り、Toggl と Notion に送る。Dot からは MCP の `timer` で測る。Toggl の直接の記録も取り込む |
| 自己改善 | `improve` | Mac の作業場で修正案を相談し、確認後にコードを直す。Dot からの修正依頼は Codex のクラウドで PR を出す |
| 声 | `voice` | Mac で出来事を読み上げる。音声対話は Dot の通話を使い、Mac のマイクも任意で使える |
| Notion | `notion` | ローカルの Notion ゲートウェイ。使う側ごとに届くホームを絞る |

- 夜間の Task は `agents.csv` で `night` をオンにしたときだけ動く。使い方と既存設定は [モジュールの説明](docs/modules.md#夜間の-task) を参照する。Dot に夜間処理を任せる構成ではオフにする。

## はじめる

- 要るもの: macOS、Claude Code か Codex の CLI、[uv](https://docs.astral.sh/uv/)、pueue
- 対話に使うもの: MCP クライアント。Dot と Slack を使う構成の接続手順は [`docs/dots.md`](docs/dots.md)
- あれば使うもの: Notion、Toggl、OpenAI（声の会話）

```zsh
git clone https://github.com/97kuek/kei-agent.git ~/src/kei-agent
cd ~/src/kei-agent
brew install pueue ffmpeg && brew services start pueue
uv sync --all-groups
uv run kei-agent setup     # 担当の表（agents.csv）・設定・プロフィール・秘密情報を作り、常駐の登録まで進む
uv run kei-agent doctor    # 困ったら点検（読むだけ）
```

## 自分のモジュールを作る

```zsh
uv run kei-agent module new weather --ai    # ひな形を作る（担当プロセスも持つなら --process）
uv run kei-agent module test weather        # 本物に触れないテスト
uv run kei-agent module add weather         # オンにする
```

## ドキュメント

| 読みたいこと | 場所 |
|---|---|
| Slack での使い方 | [`docs/using.md`](docs/using.md) |
| Dot のカスタム指示・通話・定期実行に全文コピペするプロンプト | [`docs/prompts/README.md`](docs/prompts/README.md) |
| 領域ごとの分担（研究・大学・仕事・知識） | [`docs/agents.md`](docs/agents.md) |
| 仕組み（プロセス・AI の動かし方・柵・定期実行・Notion） | [`docs/architecture.md`](docs/architecture.md) |
| モジュールの作り方 | [`docs/modules.md`](docs/modules.md) |
| 設計の決めごと | [`docs/extensibility.md`](docs/extensibility.md) |
| Dot の接続と定期処理の設定 | [`docs/dots.md`](docs/dots.md#dot-側の設定) |
| Mac の常駐・設定・更新 | [`deploy/README.md`](deploy/README.md) |
| 開発の手順 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## ライセンス

MIT（`LICENSE`）
