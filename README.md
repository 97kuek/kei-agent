# Kei Agent

Slack で頼んだ用事を、頭（OpenAI の Dot）が受けて、手（Kei Agent）の担当が作業場で進めるパーソナルアシスタント。

- Slack の受け答えと決まった時刻の処理は Dot が受け持つ。Kei Agent は自分の Mac で常駐し、Dot に MCP（手の口）で呼ばれて作業する
- Kei Agent の機能はどれもモジュールで、使うものだけをオンにする。自分のモジュールを足して作り替えられる
- 作業する AI は Claude Code か Codex の CLI。越えてはいけない線は Kei Agent がどちらにも同じ固さで守らせる

![Kei Agent の全体図](docs/images/overview.svg)

## 何ができるか

| モジュール | 名前 | できること |
|---|---|---|
| 研究 | `research` | テーマのチャンネルごとの作業場で、コード・実験・論文探しをし、研究ホーム（Notion）に残す |
| 大学 | `course` | Moodle の締切を知らせ、授業ホーム・Box の要項と過去問を読んで答える |
| 仕事 | `work` | 会社の Outlook・Teams・SharePoint を読んで答える（送信はしない）。プロジェクトのチャンネル（`#work-<名前>`）ごとの作業場で、コードを書いてテストを動かす |
| 知識 | `knowledge` | 技術記事と論文の新着を毎朝選んで要約し、その質問に答える |
| Daily・振り返り | `daily` | 朝に今日の予定と Daily、夜に振り返りを出す（いまは Dot の予定が受け持ち、このモジュールの定期処理は止めている） |
| 時間記録 | `time` | 時間を測り、Toggl と Notion に送る。Dot からは手の口の `timer` で測る。Toggl の直接の記録も取り込む |
| 自己改善 | `improve` | 要望から Kei Agent 自身のコードを直す（Slack につなぐときだけ。いまは Codex のクラウドで PR を出す） |
| 声 | `voice` | 出来事を喋って知らせる。マイクで会話もできる |
| Notion | `notion` | Notion への唯一の出入口。使う側ごとに届くホームを絞る |

## はじめる

- 要るもの: macOS、Claude Code か Codex の CLI、[uv](https://docs.astral.sh/uv/)、pueue
- 頭に使うもの: 自分用の Slack のワークスペースと、OpenAI の Dot（ChatGPT Pro）。Dot をつなぐ手順は [`docs/dots.md`](docs/dots.md)
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
| Dot（Slack の受け口と定期処理）への指示 | [`docs/dots.md`](docs/dots.md) |
| 担当（研究・大学・仕事・知識）ごとの中身 | [`docs/agents.md`](docs/agents.md) |
| 仕組み（プロセス・AI の動かし方・柵・定期実行・Notion） | [`docs/architecture.md`](docs/architecture.md) |
| モジュールの作り方 | [`docs/modules.md`](docs/modules.md) |
| 設計の決めごと | [`docs/extensibility.md`](docs/extensibility.md) |
| 入れ方と運用 | [`deploy/README.md`](deploy/README.md) |
| 開発の手順 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## ライセンス

MIT（`LICENSE`）
