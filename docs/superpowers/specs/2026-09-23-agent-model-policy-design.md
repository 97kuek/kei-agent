# エージェント文書とモデルレシピ設計

## 目的

担当・権限・実行器をエージェント単位で説明可能にし、モデル名の更新を作業内容の方針を変えずに一か所で行えるようにする。

## 文書構造

`docs/agents.md` は全体の索引と共通契約に限定する。実行の詳細は `docs/agents/` の次の文書へ分ける。

- `orchestrator.md`: Slack、A2A、状態、再試行、App Home の責務
- `research.md`: 研究ホーム、Notion Gateway、W&B、routine/standard/deep の選び方
- `course.md`: 授業ホーム、Box、Moodle、成績・単位、書込権限
- `work.md`: Microsoft 365 の読み取り専用境界
- `model-policy.md`: モデルレシピ、更新・評価・ロールバックの正本

## モデルレシピ

`config.toml` の `[model_recipes.<name>]` をモデル名と推論強度の唯一の更新点にする。レシピ名は `routine`、`standard`、`deep` で固定し、provider・model・reasoning_effort を持つ。agent は `[agents.<agent>].default_recipe` で既定のレシピを参照する。

実行時は agent の provider とレシピの provider が一致した場合だけレシピを適用する。不一致は設定エラーにする。暗黙のprovider切替や別モデルへのフォールバックはしない。

## 研究の分類

研究エージェントは依頼本文を、深い設計語→定型操作語→既定の順で分類する。

- routine: Notion/W&B の検索・記録、run/metric/artifact の確認、定型の一覧
- standard: 実装、実験実行、通常の解析と論文調査
- deep: 研究設計、仮説、実験計画、手法選択、比較設計、厳密なレビュー

深い設計語がある依頼は routine 語を含んでも `deep` を優先する。Slack の依頼先で `[[routine]]`、`[[standard]]`、`[[deep]]` を先頭に置くと、分類を明示的に上書きできる。上書き記号は実行器へ渡さず本文から除く。

大学と仕事は agent ごとの `default_recipe` を使う。定型 handler はモデルを呼ばず、自由質問だけが既定レシピで実行される。

## 更新運用

1. 新モデルを既存レシピ名へ直接置換しない。`routine-next` など検証用レシピを追加する。
2. App Home または一時設定で対象agentに検証用レシピを適用し、代表依頼で成功率・時間・利用量を比較する。
3. 合格後に固定名のレシピを更新する。問題時は前のmodel値へ戻すだけでロールバックできる。
4. providerが異なるレシピは、接続可否と権限ポリシーを先に検査する。

OpenAIモデルの例は文書上の参考であり、利用可否は実行時のCodex/Claude環境を正とする。
