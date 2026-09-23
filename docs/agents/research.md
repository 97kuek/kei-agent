# 研究エージェント

`src/kei_agent_research/`。研究テーマの作業場、pueueジョブ、研究ホーム内のNotion、W&Bを扱う。Slackの返答や再試行の約束はオーケストレーターが持つ。

## 権限と道具

- 研究テーマの作業場だけを読み書きし、Bashはsandbox内で動かす。
- Notionは `research-notion` Gateway経由で研究ホーム配下だけを操作する。生のNotionトークンは渡さない。
- W&Bは `managing-wandb` skillの範囲でrun、metric、artifact、sweepを確認・記録する。
- 長い計算はpueueへ投入する。外部ネットワークはテーマごとの許可先だけを使う。

## モデル深度

研究 agent の provider は App Home で選ぶ。書誌・固定項目・ログの抽出は Luna/low または Haiku、通常の実装・実験・調査は Sol/high または Sonnet/high、仮説・研究設計・厳密レビューは Sol/xhigh または Opus/high を使う。`[[research-extract]]`、`[[research-screen]]`、`[[research-compare]]`、`[[research-execute]]`、`[[research-design]]` は明示上書きで、記号は実行時に本文から取り除かれる。自由文は軽量分類器が判定し、不明なら通常実行に落とす。詳細は [モデル運用](../model-policy.md) を参照する。
