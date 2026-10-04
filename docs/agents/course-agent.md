# 大学（`course`）

- Moodle の締切を授業ホーム（Notion）に取り込み、知らせる
- 授業ホームと Box の学部要項・過去問を読んで、根拠付きで答える
- Toggl の記録から、科目ごとの時間を集計する
- Moodle の ICS で締切を取得し、任意の認証付き API で提出・受験終了を確認する。課題の状態は Notion で管理する
- 課題の提出の代行はしない

| 項目 | 中身 |
|---|---|
| チャンネル | `#2-course` |
| ポート | 8787 |
| フォルダ | `modules/course/`（Moodle・Notion の機械処理、学校別の部品は `schools/`） |
| Mac の役割 | Moodle の締切・提出状態の取得と Notion への同期。AI は実行しない |
| クラウドの役割 | Dot が Notion の授業・課題・成績・単位要件と Box の資料を直接読み、必要な Notion の更新も行う |

## 頼み方

| 言い方 | 起きること |
|---|---|
| 「課題を取り込んで」 | Moodle の締切を「課題」に入れる（履修している科目だけ） |
| 「提出状態を確認して」 | Moodle の提出・受験終了を確認し、Notion の対応する課題の Status を Submitted にする |
| 「締切を教えて」 | これから2週間ぶんを近い順に。「一番近い」なら1件 |
| 「今週どれくらいやった？」 | Dot が Notion の時間記録を科目ごとに集計する |
| そのほか | Dot が Box の要項・過去問と授業ホームを直接読んで答える |

## 自動でしていること

| いつ | 中身 |
|---|---|
| Mac の起動・復帰後と稼働中30分ごと | Moodle の課題を取り込み、新しい課題と締切の変わった課題を `#2-course` に知らせる |
| 10分ごと（API 設定時） | 最大10件の提出・受験状態を確認する。続きの位置を記録して順番に確認し、変更した課題だけ知らせる |
| 08:00 以降に1回・提出状態の変更時 | これからの課題と状態を共通ホームの予定カレンダーに写す |
| 朝の一覧 | その日の授業（時刻つき）と締切を渡す |

- 締切通知は Dot の予定が配信する（[dots.md](../dots.md#締切の知らせ08001800)）
- 締切の「0:00」は、前の日の「24:00」として出す

Mac の見回りが5分以上空いた場合に復帰として同期する。取得失敗は10分後に再試行し、最終成功時刻を更新しない。締切と提出の全同期が同時に要求されても、同じ処理を共有する。API 未設定の場合、提出状態は確認できず、締切の取り込みだけ行う。

## クラウドでの回答

Notion・Box の閲覧や Notion の更新に Mac は不要。Dot から `run(workspace="course")` は使わない。Notion の課題状態は Mac の最終同期時点の情報なので、最新の提出状況が取得できていなければその旨を伝える。

## スキル

| スキル | 中身 | AI |
|---|---|---|
| `sync-assignments` | Moodle から課題を取り込む | 使わない |
| `sync-submissions` | 課題の提出・小テストの受験終了を確認して Notion に反映する | 使わない |
| `list-due` | 締切の近い課題（既定2週間。`days` で変える） | 使わない |
| `list-calendar-assignments` | 予定カレンダー用に、期間の課題を全部読む | 使わない |
| `list-classes` | その日の授業（既定は今日。`weekday` で変える） | 使わない |
| `list-current-courses` | 今学期の履修科目 | 使わない |
| `time-report` | Toggl の記録を科目・課題ごとに集計（既定7日） | 使わない |


## 授業ホーム（Notion）

上から順に置く。列の名前と選択肢は英語で、列は表の順（名前 → よく見る列 → 時間と数字 → つながりと照合キー）。表のビューも同じ順にそろえる。

| 置くもの | 列（左から） |
|---|---|
| 授業時間表（ページ） | 何限が何時から何時か。Dot は授業の時刻をここから換算する |
| 授業 | Name・Status（Taking / Done）・Year・Term・Day・Period・Credits・Moodle。成績とは Name＋Year＋Term で突き合わせる |
| 課題 | Name・Status（Not started / In progress / Submitted / Overdue）・Due・Course・Link・Moodle ID（空なら手入力の課題） |
| 成績 | Name・Grade・GP・Credits・Category（科目区分）・Group（科目群）・Course・Requirement・Record ID |
| 単位要件 | Name・Remaining・Required・Counted・Group（大区分）・Kind（Category / Subtotal / Total / Other）・Grades・Record ID |

- 取得済みかは Grade が F でないことで見る。GPA は保存せず、Dot が成績から計算する（GP が空の行は除き、GP×Credits の合計÷Credits の合計）。大学の公式の GPA と小数点以下がずれることがある
- 単位の残りは単位要件の Remaining（Kind が Total の行が卒業要件の全体）。既得単位は取り込まない
- 勉強時間は共通ホームの「時間記録」（領域＝大学）で持つ
- 既にある授業ホームでは、API で授業時間表のページを先頭に挿せない。`setup` のあと、授業時間表を手で一度いちばん上へ動かす
- `setup` は、題の列が Name でない DB や「📊 成績履歴」「🎓 単位要件」が残るホームには何も書かずに止まる
- 課題の Status を手で Overdue や Submitted にしても、Moodle の取り込みは変えない

## コマンド

```zsh
uv run kei-agent-module course setup --seed ~/.config/kei-agent/courses.toml   # 授業時間表のページと4つの DB をそろえ、履修科目を入れる
uv run kei-agent-module course sync                  # 手で締切を取り込む（--all で履修外も）
uv run kei-agent-module course sync-submissions      # 最大10件の提出・受験終了を確認する（再実行で続き）
uv run kei-agent-module course inspect               # Moodle と「授業」を読むだけで照合する
uv run kei-agent-module course academic-import --dry-run <grades.html> <credits.html>   # 成績と単位（--apply で書く）
```

- 履修科目のファイルの書き方は `modules/course/courses.example.toml`
- 成績のファイルはローカルで読み、Notion には置かない（`--delete-inputs` で消す）
- 成績に当たる授業が「授業」に無ければ、終わった授業（Status＝Done）として足して結ぶ。単位要件は成績の Requirement で結ぶ

## 設定と秘密情報

| 名前 | 場所 | 中身 |
|---|---|---|
| `school` | `config.toml` の `[course]` | 学校の部品（同梱は `waseda`）。自分の部品はファイルの場所 |
| `periods` / `terms` | `config.toml` の `[course]` | 時限の時刻・学期。書かなければ学校の部品の既定 |
| `MOODLE_ICS_URL` | `kei-agent-course.zsh` | Moodle のカレンダーの書き出し URL |
| `MOODLE_API_URL` / `MOODLE_API_TOKEN` | `kei-agent-course.zsh` | 任意。HTTPS の Moodle ルート URL と利用者本人の API トークン。両方そろえて使う |
| Box・Notion の接続 | Dot のプラグイン | 大学の資料・授業ホームにアクセスできるアカウント |

- 学校の部品に置けるもの（時限・学期の既定、成績の読み方）は `modules/course/school.py`
- 学校を選ばず時刻も書かないと、授業に時刻が付かない（朝の一覧に出ない）

## Moodle の提出・受験終了の同期

1. 学校側で REST API と次の関数を使えるサービスを用意し、本人のトークンを取得する。利用できない場合は学校の管理者への確認が必要
2. `kei-agent-course.zsh` に `MOODLE_API_URL`（例 `https://moodle.example/moodle`）と `MOODLE_API_TOKEN` を設定し、担当プロセスを再起動する。値はチャットや Notion に書かない
3. 上の `sync-submissions` コマンド、または Dot の MCP `sync_submissions` で確認する。API が未設定なら `enabled=false` を返し、Notion は変更しない

| API 関数 | 用途 |
|---|---|
| `core_calendar_get_calendar_event_by_id` | ICS に活動 URL がない場合、イベント ID から活動 URL を取得 |
| `core_course_get_course_module` | URL の活動 ID（cmid）を課題・小テストのインスタンス ID に変換 |
| `mod_assign_get_submission_status` | `userid=0` でトークン所有者の課題提出状態を確認 |
| `mod_quiz_get_user_attempts` | `userid=0` でトークン所有者の小テスト受験状態を確認 |

- API が返す対象は `userid=0`（トークン所有者）。ICS の利用者との本人照合は行わないため、締切を取得する利用者本人の API トークンを設定する
- 課題は `submitted`、小テストはプレビュー以外の受験が `finished` または `submitted` のときだけ Status を Submitted にする。グループ課題は本人の提出済み、またはグループ提出済みで提出待ちのメンバーがいない場合に限る
- Notion の Moodle ID・活動 URL と API の活動 ID・種類を照合する。URL は設定した Moodle のものだけ使い、タイトルの類似では照合しない
- 未提出・下書き・受験途中・取得失敗では状態を戻さない。Status が Submitted の行と手入力の課題（Moodle ID が空）は確認対象から外す。Moodle ID の重複があれば書き込まない
- 判別できない活動はそのまま残す。確認件数が10件を超える場合、全件の確認には複数回の見回りが必要。Mac を閉じている間は同期しない
- 締切一覧は Notion の Status を参照し、Submitted と Overdue を除く。Dot の締切通知も同じ Status を見る

API の判定は Moodle 公式の [課題 API](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/externallib.php)、[小テスト API](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/quiz/classes/external.php)、[カレンダー API](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/calendar/externallib.php)、[活動 API](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/course/externallib.php) に基づく。学校で使える関数はサービスの権限による。
