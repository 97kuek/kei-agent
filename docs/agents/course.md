# 大学エージェント

`src/kei_agent_course/`。授業ホームの成績・GPA・単位要件、今学期の履修、Moodle、Boxを扱う。

## 権限

- Notionは授業ホーム内で読取・作成・更新・移動・複製・アーカイブ・DB作成を行える。
- Boxは大学アカウントの資料を読むだけ。アップロードや共有設定変更は行わない。
- Moodleは課題・締切の同期に使う。学習時間はToggl確定後に授業ホームへ記録する。

Moodle の秋冬科目を確かめるときは `kei-agent-course-inspect` を使う。このコマンドは ICS と既存の「授業」DBを読むだけで、新しい授業・課題・relation は作らない。曜日、時限、履修状態は ICS だけでは確定しないため、確認が必要な科目として報告する。

## 授業ホームの DB とレイアウト

正本は `授業`、`課題`、`学習ログ`、`📊 成績履歴`、`🎓 単位要件`、`📈 GPA推移` の6 DBだけである。`Untitled` など名前が一致しない DB は、内容を推測せず今回の同期・レイアウト移行の対象外とする。

`kei-agent-course-setup` は既存の正本 DB に不足する stable ID と relation だけを追加する。課題 DB の title property は、既存の同じ property ID を `タイトル` から `課題` に改名する。二つ目の title property は作らない。

```zsh
source ~/.config/zsh/local/kei-agent-course.zsh
uv run --group course kei-agent-course-layout
uv run --group course kei-agent-course-layout --apply
```

最初のコマンドは dry-run で、`課題一覧` / `GPA推移` view の作成・更新、正規化する課題名、空本文へ足す整理見出しの件数だけを表示する。内容を確認してから `--apply` を付ける。課題名は厳密に `「…」の提出期限` のときだけ `…` に短縮し、既存本文がある課題ページは変更しない。GPA chart は既存の `期間` と `GPA` の値だけを線で表示し、値や relation を補完しない。

## モデル方針

`sync-assignments`、`list-due`、`list-classes`、成績・単位の定型計算はコードとNotion APIで処理し、モデルを呼ばない。Box資料とNotionを横断する `ask` は、軽い説明を `course_explain`、要件整理を `course_requirements`、比較を `course_compare`、履修・卒業計画を `course_degree_plan` として model policy の recipe から選ぶ。

モデル名を個別のskillへ埋め込まない。更新は [モデル運用](../model-policy.md) のレシピを変える。
