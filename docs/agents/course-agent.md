# 大学（`course`）

- Moodle の締切を授業ホーム（Notion）に取り込み、知らせる
- 授業ホームと Box の学部要項・過去問を読んで、根拠付きで答える
- Toggl の記録から、科目ごとの時間を集計する
- 課題の提出の代行はしない

| 項目 | 中身 |
|---|---|
| チャンネル | `#2-course` |
| 番地 | 8787 |
| フォルダ | `modules/course/`（指示書 `course.md`、skill は `plugin/`、学校の部品は `schools/`） |
| 作業場 | `~/course`（資料は Box に置いたまま読む） |
| 触れる範囲 | 作業場を読み書き・コマンド・Web・授業ホームを読み書き・Box を読むだけ（[制限の表](../agents.md#触れる範囲制限の表)） |

## 頼み方

| 言い方 | 起きること |
|---|---|
| 「課題を取り込んで」 | Moodle の締切を「課題」に入れる（履修している科目だけ） |
| 「締切を教えて」 | これから2週間ぶんを近い順に。「一番近い」なら1件 |
| 「今週どれくらいやった？」 | Toggl の記録を科目ごとに集計する |
| そのほか | Box の要項・過去問と授業ホームを読んで答える |

## 自動でしていること

| いつ | 中身 |
|---|---|
| Daily と振り返りの前 | Moodle の課題を取り込み、新しい課題と締切の変わった課題を `#2-course` に知らせる |
| 1時間ごと | 締切の24時間前と、3日前でも「未着手」の課題を一度だけ知らせる |
| 08:00 以降に1回 | これからの課題を共通ホームの予定カレンダーに写す |
| 朝の一覧 | その日の授業（時刻つき）と締切を渡す |

- 締切の「0:00」は、前の日の「24:00」として出す

## AI の用途

| 用途 | 使うとき | Claude | Codex |
|---|---|---|---|
| `course_explain`（既定） | 1つの資料の説明 | sonnet-5 / medium | luna / medium |
| `course_requirements` | 課題の要件・評価基準の整理 | sonnet-5 / high | sol / medium |
| `course_compare` | 複数の資料・試験範囲の比較 | sonnet-5 / high | sol / medium |
| `course_degree_plan` | 履修・卒業の計画 | opus-5 / high | sol / xhigh |

## スキル

| スキル | 中身 | AI |
|---|---|---|
| `sync-assignments` | Moodle から課題を取り込む | 使わない |
| `list-due` | 締切の近い課題（既定2週間。`days` で変える） | 使わない |
| `list-calendar-assignments` | 予定カレンダー用に、期間の課題を全部読む | 使わない |
| `list-classes` | その日の授業（既定は今日。`weekday` で変える） | 使わない |
| `list-current-courses` | 今学期の履修科目 | 使わない |
| `time-report` | Toggl の記録を科目・課題ごとに集計（既定7日） | 使わない |
| `ask` | 定型に当たらない質問 | 使う |

- skill（`plugin/skills/`）: `finding-course-materials`、`managing-assignments`、`managing-academic-record`、`managing-course-notion`

## 授業ホーム（Notion）

| DB | 中身 |
|---|---|
| 授業 | 科目名、科目コード、学期、曜日、時限、Moodle、状態、科目群・科目区分・必選区分 |
| 課題 | 課題名、締切、状態、授業への relation |
| 📊 成績履歴 | 科目群、科目区分、授業名、成績、GP、単位、取得年度 |
| 🎓 単位要件 | 大区分、要件名、所定・既得・算入・残り単位（`総合計` が卒業要件の全体） |
| 📈 GPA推移 | 春学期・秋学期・通算の GPA |

## コマンド

```zsh
uv run kei-agent-module course setup --seed ~/.config/kei-agent/courses.toml   # 5つの DB をそろえ、履修科目を入れる
uv run kei-agent-module course sync                  # 手で締切を取り込む（--all で履修外も）
uv run kei-agent-module course inspect               # Moodle と「授業」を読むだけで照合する
uv run kei-agent-module course academic-import --dry-run <grades.html> <credits.html>   # 成績と単位（--apply で書く）
```

- 履修科目のファイルの書き方は `modules/course/courses.example.toml`
- 成績のファイルはローカルで読み、Notion には置かない（`--delete-inputs` で消す）

## 設定と秘密情報

| 名前 | 場所 | 中身 |
|---|---|---|
| `school` | `config.toml` の `[course]` | 学校の部品（同梱は `waseda`）。自分の部品はファイルの場所 |
| `periods` / `terms` | `config.toml` の `[course]` | 時限の時刻・学期。書かなければ学校の部品の既定 |
| `MOODLE_ICS_URL` | `kei-agent-course.zsh` | Moodle のカレンダーの書き出し URL |
| `claude_account` / `codex_account` | `agents.csv` の course の行 | Box をつないだ個人アカウントのフォルダ |

- 学校の部品に置けるもの（時限・学期の既定、成績の読み方）は `modules/course/school.py`
- 学校を選ばず時刻も書かないと、授業に時刻が付かない（朝の一覧に出ない）
