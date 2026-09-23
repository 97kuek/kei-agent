---
name: managing-academic-record
description: Use when大学エージェントがGPA、取得単位、卒業要件、成績履歴をNotionの授業ホームで確認・更新するとき。
---

# 学業記録を扱う

授業ホーム配下の次のDBを正として扱う。

- `📊 成績履歴`: 科目ごとの年度・学期・単位・成績・GP
- `🎓 単位要件`: 所定・既得・算入・残り単位。`総合計` 行を先に確認する
- `📈 GPA推移`: 学期別と通算のGPA。`通算` 行を現在値として答える

## 質問への答え方

1. まず対象DBを `notion-query-data-sources` で検索する
2. GPAは `📈 GPA推移` の `種別 = 通算`、取得単位と卒業までの残りは `🎓 単位要件` の `集計種別 = 総合計` を使う
3. DBに無い推測値は作らず、「Notionの取り込み済み記録では不明」と返す
4. 科目単位の質問は `📊 成績履歴` を年度・学期・科目区分で絞る

原HTMLは Notion に保存しない。再取り込みは必ず次の順で行う。

1. `kei-agent-course-academic-import --dry-run <grades.html> <credits.html>` で件数だけ確認する（書き込まない）
2. `--apply` を明示して stable ID で作成・更新・未変更を検証する
3. 入力を消してよいと依頼者が明示したときだけ `--apply --delete-inputs` を使う

成績と科目台帳の relation が曖昧と表示されたときは、推測で結ばず手で確認する。
