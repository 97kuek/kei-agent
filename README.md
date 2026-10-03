# Kei Agent

Kei Agent は、対話からローカルの作業実行までをつなぐ、モジュール式のアシスタント。使う機能・AI・アカウントを設定し、自分の用途に合わせて拡張できます。

- MCP クライアントから、作業の依頼・進捗確認・通知の取得ができます。Dot は任意のクライアントです
- 共通コードが実行管理・権限・状態保存を受け持ち、領域ごとの機能はモジュールとして追加します
- 個人のプロフィール・接続情報・モジュールの選択はリポジトリの外に置きます
- Slack・通話 → Dot → MCP → Mac の構成と、コピーして使える Dot 用プロンプトを同梱しています。通話の Slack 記録は利用者の依頼と投稿権限に従います

下の図と表のクラウド連携は、Dot を使う構成例です。共通部分とクライアントの役割は [設計の決めごと](docs/extensibility.md) を参照してください。

![Kei Agent の全体図](docs/images/overview.svg)

## したいことから読む

| したいこと | 読む場所 |
|---|---|
| Slack・声で頼む、時間を記録する | [依頼の使い方](docs/using.md) |
| 研究テーマを作り、調査・実験を進める | [研究](docs/agents/research-agent.md) |
| 授業・課題・Moodle を確認する | [大学](docs/agents/course-agent.md) |
| 記事や論文を探し、保存する | [知識](docs/agents/knowledge-agent.md) |
| 会社の資料を読む、コードを変更する | [仕事・コード変更](docs/agents/work-agent.md) |
| 個人のアプリ・サイト・Kei Agent をクラウドで開発する | [個人開発](docs/agents/development-agent.md) |
| Dot を接続し、定期実行を設定する | [接続・予定](docs/dots.md) → [コピーするプロンプト](docs/prompts/README.md) |
| 機能を足す | [モジュールの契約と作り方](docs/modules.md) |
| Mac を導入・更新・点検する | [運用手順](deploy/README.md) |

共通の技術仕様は [実行基盤](docs/architecture.md)、設計の理由は [設計方針](docs/extensibility.md)、開発手順は [CONTRIBUTING.md](CONTRIBUTING.md)。

## 何ができるか

| モジュール | 名前 | できること |
|---|---|---|
| 研究 | `research` | テーマのチャンネルごとの作業場で、コード・実験・論文探しをし、研究ホーム（Notion）に残す |
| 大学 | `course` | Moodle の締切と、API 設定時の提出状態を Notion へ機械同期する。授業・Box の質問は Dot |
| 仕事 | `work` | 会社の資料を読み、Mac のプロジェクトでコードとテストを実行する。Outlook は Dot |
| 知識 | `knowledge` | 既存記事の保存・解除の互換処理。新しい記事の検索・要約・保存・配信は Dot |
| Daily・振り返り | `daily` | 朝に今日の予定と Daily、夜に振り返りを出す（Dot の予定を使う構成では、このモジュールの定期処理を止める） |
| 夜間の Task | `night` | Notion の 🌙 Task を研究担当に渡し、結果と確認待ちを記録する（必要な構成だけでオンにする） |
| 時間記録 | `time` | 時間を測り、Toggl と Notion に送る。Dot からは MCP の `timer` で測る。Toggl の直接の記録も取り込む |
| 自己改善 | `improve` | Mac の作業場で修正案を相談し、確認後にコードを直す（クラウドの PR 運用は外部設定） |
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

## ライセンス

MIT（`LICENSE`）
