# 研究エージェント

`src/kei_agent_research/`。研究テーマの作業場、pueueジョブ、研究ホーム内のNotion、W&Bを扱う。Slackの返答や再試行の約束はオーケストレーターが持つ。

## 権限と道具

- 研究テーマの作業場だけを読み書きし、Bashはsandbox内で動かす。
- Notionは `research-notion` Gateway経由で研究ホーム配下だけを操作する。生のNotionトークンは渡さない。
- W&Bは `managing-wandb` skillの範囲でrun、metric、artifact、sweepを確認・記録する。
- 長い計算はpueueへ投入する。外部ネットワークはテーマごとの許可先だけを使う。

## モデル深度

| 深度 | 対象 | 既定レシピ |
|---|---|---|
| routine | Notion/W&Bの検索・記録、run/metric/artifact確認、定型一覧 | `routine` |
| standard | 実装、実験実行、通常の解析、論文調査 | `standard` |
| deep | 研究設計、仮説、実験計画、手法選択、比較設計、厳密なレビュー | `deep` |

深い設計語があればroutine語が含まれていても `deep` を優先する。先頭に `[[routine]]`、`[[standard]]`、`[[deep]]` を置けば明示的に選べる。記号は実行時に本文から取り除かれる。

モデルレシピ名は安定させ、実モデル名は [モデル運用](../model-policy.md) と `config.toml` の `[model_recipes]` で更新する。
