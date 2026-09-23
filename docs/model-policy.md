# モデル運用

モデル名をagentやskillへ散らさず、`config.toml` の `[model_recipes]` を唯一の更新点にする。レシピ名と意味は固定し、モデル世代だけを置換する。

| レシピ | 用途 | 現在の意図 |
|---|---|---|
| `routine` | 定型検索・記録・抽出 | 低遅延・低コスト |
| `standard` | 通常の実装・調査・横断要約 | 品質と速度の均衡 |
| `deep` | 研究設計・厳密レビュー・複雑な判断 | 品質優先 |

各レシピにはprovider、model、reasoning_effortを記録する。agentのproviderと一致しないレシピはエラーにし、暗黙のprovider切替や自動フォールバックをしない。

## 更新手順

1. 新モデルはまず `routine-next` のような検証用レシピとして追加する。
2. 代表依頼で成功率、所要時間、利用量、回答の根拠を比較する。
3. 十分なら固定名のレシピだけを新モデルへ置換する。
4. 問題があれば前のmodel値へ戻す。agent・skill・promptを同時に変えない。

OpenAIでは、定型処理に軽量モデル、日常の判断を伴う作業に中位、難しい分析に最上位を割り当て、代表ワークロードで評価することが推奨されている。[Model Selection](https://developers.openai.com/api/docs/guides/model-selection)
