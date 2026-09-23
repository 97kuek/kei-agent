# モデル運用

Claude と Codex は対等な provider であり、全体の既定 provider はない。App Home で agent ごとに provider だけを選び、model と effort は実行時にユースケース別 recipe から決める。skill は所属 agent の recipe を継承する。

通常実行で許可する model は次だけである。

| provider | allowlist |
|---|---|
| Codex | `gpt-6-luna` / `gpt-6-sol` / `gpt-6-astra` |
| Claude | `claude-haiku-4-5` / `claude-sonnet-5` / `claude-opus-5` / `claude-fable-5` |

`gpt-5.6-*` は使わない。Astra/Fable は通常 recipe に割り当てず、依頼者が明示した `[[manual-astra]]` / `[[manual-fable]]` だけで使う。provider 未選択、connector 不足、quota 到達、または実行経路が effort を受け付けない場合は理由を表示して停止する。別 provider・上位 model への自動フォールバックはしない。

## recipe

router と自由文の軽量分類は Codex では Luna/low、Claude では Haiku 4.5（thinking なし）。研究の抽出は同じ軽量枠、通常実行は Sol/high または Sonnet/high、研究設計は Sol/xhigh または Opus/high を使う。大学、仕事、Daily/Retro、自己改善も同じく `src/kei_agent/model_policy.py` の provider 別 table を唯一の実装上の正とする。

free-form `ask` は選択済み provider の軽量分類器が用途を判定する。JSON が不正、信頼度が低い、または分類器が技術的に失敗したときは、その agent の通常 recipe に落とす。分類器を強い model で再試行しない。ただし quota 到達は provider の停止としてその場で報告し、通常 recipe に進まない。`[[research-design]]` のような明示ラベルは分類器より優先し、本文から取り除く。

## 評価と変更

変更前に provider・ユースケースごとの代表入力で、必須事実、根拠、不明点の扱い、必要 tool、所要時間と quota を確認する。不合格なら、その provider のそのユースケースだけを effort または model で引き上げる。別 provider の結果を流用しない。
