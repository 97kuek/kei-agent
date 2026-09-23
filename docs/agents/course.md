# 大学エージェント

`src/kei_agent_course/`。授業ホームの成績・GPA・単位要件、今学期の履修、Moodle、Boxを扱う。

## 権限

- Notionは授業ホーム内で読取・作成・更新・移動・複製・アーカイブ・DB作成を行える。
- Boxは大学アカウントの資料を読むだけ。アップロードや共有設定変更は行わない。
- Moodleは課題・締切の同期に使う。学習時間はToggl確定後に授業ホームへ記録する。

## モデル方針

`sync-assignments`、`list-due`、`list-classes`、成績・単位の定型計算はコードとNotion APIで処理し、モデルを呼ばない。Box資料とNotionを横断する `ask` は、軽い説明を `course_explain`、要件整理を `course_requirements`、比較を `course_compare`、履修・卒業計画を `course_degree_plan` として model policy の recipe から選ぶ。

モデル名を個別のskillへ埋め込まない。更新は [モデル運用](../model-policy.md) のレシピを変える。
