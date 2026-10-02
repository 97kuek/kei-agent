# 研究（`research`）

- 研究テーマのチャンネルで、そのテーマの作業場を使って作業する
- コードを書く・実験を回す・論文を探す・研究ホーム（Notion）に残す
- 長い処理はジョブにして、終わったら同じスレッドで報告する

| 項目 | 中身 |
|---|---|
| チャンネル | ほかのどれでもないチャンネル（`#1-<テーマ>` など）。`module.toml` の `[channels]` の `theme = ["*"]` |
| 番地 | 8788 |
| フォルダ | `modules/research/`（指示書 `research.md`、skill は `plugin/`） |
| 触れる範囲 | 作業場を読み書き・コマンド・Web・研究ホーム（[制限の表](../agents.md#触れる範囲制限の表)） |

## テーマの作業場

- チャンネルに招くと作業場・`AGENTS.md` のひな形（と、それを読み込む `CLAUDE.md`）・Notion の「テーマ」の行ができる
- 招いたときに [既定の場所に作る] か [既存のフォルダを使う] を選ぶ

```text
~/research/<テーマ>/     既定の置き場所（agents.csv の research の行の folder）
├── AGENTS.md            前提・分野・## 検索キーワード・ジョブにする基準（Claude と Codex が読む）
├── CLAUDE.md            AGENTS.md を読み込む1行
├── inputs/              添付されたファイル
├── outputs/             見せたい図や集計（新しいものはスレッドに添付）
├── logs/                ジョブのログ
└── .kei-agent/          スレッドのログとジョブの状態（Kei Agent が書く）
```

| 既存のフォルダを使うとき | 扱い |
|---|---|
| 対応の置き場所 | `~/.config/kei-agent/themes.toml` に「テーマの名前 = フォルダ」。書き換えると読み直す |
| 前提のメモ | `AGENTS.md` か `CLAUDE.md` があれば、動かさずにそのまま使う（`CLAUDE.md` だけなら Codex には渡らない）。どちらも無いときだけ `AGENTS.md` のひな形を作る |
| `.kei-agent/` | Git のリポジトリなら `.git/info/exclude` に入れる |
| `inputs/` など | 初めて使うときに作る |
| 毎晩の保存 | しない（その人の Git に任せる） |
| 保護フォルダ（書類など） | `allow_protected_folders = true` と、フルディスクアクセスの許可が要る |

## 頼み方

| 頼み方 | 起きること |
|---|---|
| `@Kei Agent 〜して` | 作業場で AI を動かして答える |
| ファイルを添付 | `inputs/` に入る |
| 依頼の頭に `[[research-design]]` など | 用途（深さ）を指定する。下の表 |
| 🌙 を付ける | 今夜 00:00 に Task として動かす |
| チャンネルをアーカイブ | 定期処理がそのテーマを見なくなる |

## AI の用途

| 用途 | 使うとき | Claude | Codex |
|---|---|---|---|
| `research_extract` | 書誌・固定項目・ログの抜き出し | haiku-4-5 | luna / low |
| `research_screen` | はっきりした基準での仕分け | haiku-4-5 | luna / low |
| `research_compare` | 比較・結果の分析 | sonnet-5 / high | sol / medium |
| `research_execute`（既定） | 実験コード・データ処理・ふつうの調査 | sonnet-5 / high | sol / high |
| `research_design` | 仮説・実験計画・手法選び・厳しいレビュー | opus-5 / high | sol / xhigh |
| `manual_fable` | `[[manual-fable]]` と書いたときだけ | fable-5 / high | — |
| `manual_astra` | `[[manual-astra]]` と書いたときだけ | — | astra / xhigh |

## スキル

| スキル | 中身 |
|---|---|
| `ask` | テーマの作業場で AI を1回動かす |
| `submit-job` | ジョブを pueue に入れる（作業場か研究全体の作業場の中だけ） |
| `list-jobs` / `cancel-job` / `forget-job` | ジョブの状態を見る・止める・片づける |

- skill（`plugin/skills/`）: `researching-literature`（論文探しと先行研究 DB）、`running-jobs`、`managing-research-notion`、`managing-wandb`

## ジョブ

- 数分以上かかる処理は、作業場の中のスクリプトを pueue（グループ `kei-agent`）で動かす
- `--expect <ファイル>` で、できるはずのファイルを宣言する。終わったときに照合する
- 本体が毎分状態を見て、終わったらその会話を続ける。ログの末尾は、進捗だけの行をまとめ、末尾に入らなかったエラーも添えて渡す
- 失敗したら、原因と直し方を報告して、入れ直してよいか聞く（頼まれるまで入れ直さない）
- 失敗や確認待ちを24時間放っておくと、一度だけ声をかける

## 通信

- Web もコマンドの通信も、どこへでも出られる（自分のデータを読む担当。[architecture.md](../architecture.md) の柵の表）

## 研究ホーム（Notion）

- 作るのは `uv run kei-agent-notion-setup --apply`（付けなければ、作るものを並べるだけ）

| DB | 主な項目 |
|---|---|
| テーマ | 名前（チャンネル名と同じ）、状態、目的、Slack、ディレクトリ |
| Task | タイトル、テーマ、状態（未着手 / 今夜やる / 実行中 / 確認待ち / 完了）、担当、優先度、期日、結果 |
| ノート | タイトル、種類（計画 / 考察 / 議論メモ）、テーマ、日付 |
| マイルストーン | 名前、期日、テーマ、状態 |
| 先行研究 | 名前、テーマ、URL、ID（`arXiv:…`）、要点、この研究との関係、状態（未読 / 読んだ / 使う） |

- 🌙 は Task を作る入口。00:00 に「今夜やる」を1件ずつ、一晩5件まで動かし、「状態」と「結果」を書く
- テーマの前提と検索キーワードの正本は `AGENTS.md`、論文の正本は先行研究 DB
- スレッドやジョブの状態は本体の SQLite が正本で、Notion には置かない

## 設定と秘密情報

| 名前 | 場所 | 中身 |
|---|---|---|
| `folder`（research の行） | `agents.csv` | テーマのフォルダを置く場所（既定 `~/research`） |
| `allow_protected_folders` | `config.toml` | 保護フォルダを選べるようにする |
| `S2_API_KEY`（任意） | `kei-agent-research.zsh` | Semantic Scholar の鍵。論文を探す回数の上限が上がる |

- 研究をオフにすると、研究テーマのチャンネルには答えない。自分のモジュールに `theme = ["*"]` を持たせれば代わりに受け持てる
