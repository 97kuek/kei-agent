# ユースケース別モデル方針設計

## 状態

設計合意済み。実装・設定変更・モデル移行はまだ行わない。

## 目的

Kei Agent は Claude と Codex を対等な実行 provider として扱う。ただし provider 間のモデル性能、effort、connector、権限を同等とは仮定しない。agent は provider を明示選択し、model と effort はユースケースごとの provider 固有レシピから解決する。

モデルを選ぶことは、権限を選ぶことではない。agent が作業場、connector、読み書き権限を持ち、レシピは必要な判断能力・遅延・quota を決める。

## 共通契約

- `codex` の通常実行 allowlist は `gpt-6-luna`、`gpt-6-sol`、`gpt-6-astra` のみとする。`gpt-5.6-*` は許可しない。
- `claude` の通常実行 allowlist は `claude-fable-5`、`claude-opus-5`、`claude-sonnet-5`、`claude-haiku-4-5` のみとする。
- App Home などで agent ごとに選ぶのは provider だけである。model/effort の永続的な agent 上書きは持たない。
- 実行前に、選択 provider が当該ユースケースに必要な connector、権限、model、effort を実際に提供できることを検査する。不足時は理由を出して停止し、別 provider・別モデルへ自動フォールバックしない。
- Astra/Fable は allowlist に残すが通常レシピに割り当てない。依頼単位の明示指定だけで使う例外枠とする。
- provider 間で同じ effort 名や同じ用途のレシピを性能同等と説明しない。各 provider のレシピは独立して評価する。
- quota 到達時は依頼を停止し、provider、model、ユースケース、再開見込みだけを通知する。自動フォールバックはしない。

## effort の扱い

effort は provider 固有の設定である。Codex は各 GPT-6 モデルが受け付ける effort だけを渡す。Claude は実行経路が対応する adaptive-thinking/effort 設定へ変換し、Haiku 4.5 には thinking なしを明示する。変換不能な provider/model/経路は実行不可にする。

Realtime は通常 agent の effort レシピを共有しない。`gpt-realtime-2.1-mini` 固有のセッション設定だけを使う。

## ユースケースの解決

固定 skill は呼び出し元がユースケースを明示する。Moodle 同期、GPA・単位計算、時間記録、Toggl 配送、Notion Gateway、A2A 配送、保守のような決定的処理は model-free として解決する。

自由文の `ask` は、選択済み provider の軽量分類器でユースケース候補を選ぶ。Codex は Luna / low、Claude は Haiku 4.5 / thinking なしを使う。分類結果が未知、形式不正、または明確な低リスク候補でないときは、低コスト枠に落とさず当該 agent の通常レシピを使う。利用者は `[[course-requirements]]` のようなユースケースラベルで分類を上書きできる。上書きラベルはモデルへ渡す本文から取り除く。

## ユースケース別レシピ

各行の Codex と Claude は代替候補であり、性能同等の主張ではない。

| ユースケース | Codex | Claude | 制約 |
|---|---|---|---|
| router の短い JSON 分類 | Luna / low | Haiku 4.5 / thinking なし | 失敗・低信頼なら `ask`、昇格再試行なし |
| 研究: 書誌・固定項目・タグ・重複候補 | Luna / low | Haiku 4.5 / thinking なし | 候補だけ。研究上の採否・主張・引用確定に使わない |
| 研究: 明確な基準による一次仕分け | Luna / medium | Haiku 4.5 / thinking なし | 下書き・候補だけ |
| 研究: 文献比較、結果の分析 | Sol / medium | Sonnet 5 / medium | 根拠を分けて示す |
| 研究: 実験コード、データ処理、通常調査 | Sol / high | Sonnet 5 / high | |
| 研究: 仮説、実験計画、手法選択、厳密レビュー | Sol / xhigh | Opus 5 / high | |
| 大学/仕事: API で確定できる一覧、計算、同期、記録 | モデルなし | モデルなし | GPA・単位判定・締切一覧をモデルで確定しない |
| 大学: 課題要件・評価基準の整理 | Luna / high | Sonnet 5 / high | 資料名とページ/見出しを出し、原文確認が必要と表示 |
| 大学: 1資料の説明・学習支援 | Luna / medium | Sonnet 5 / medium | 資料外は推測として分ける |
| 大学: 複数資料の比較・試験範囲整理 | Sol / medium | Sonnet 5 / high | |
| 大学: 履修・卒業計画 | Sol / xhigh | Opus 5 / high | 計算はコード。モデルは選択肢の提案だけ |
| 仕事: 1件のメール/資料の要点、期限、依頼の整理 | Luna / medium | Sonnet 5 / medium | 外部書込みなし |
| 仕事: 複数メール/予定/資料の状況要約 | Sol / medium | Sonnet 5 / high | |
| 仕事: 優先順位、会議準備、論点整理 | Sol / high | Opus 5 / high | 提案だけ。外部書込みなし |
| 自己改善: 要件整理・変更設計・影響分析 | Sol / xhigh | Opus 5 / high | worktree、テスト、利用者承認を維持 |
| 自己改善: 実装・テスト修正 | Sol / high | Sonnet 5 / high | |
| 自己改善: 通常差分レビュー | Sol / medium | Sonnet 5 / medium〜high | |
| 横断: Daily/Retro の短い下書き | Luna / medium | Sonnet 5 / medium | 下書きだけ |
| 横断: 研究・大学・仕事の優先順位、週次計画 | Sol / high | Opus 5 / high | |

夜間 Task は固有レシピを持たない。元の agent とユースケースのレシピを継承する。時間記録カード、Toggl 配送、Notion Gateway、A2A 配送、保守、App Home の設定は決定的処理であり、モデルを呼ばない。

## 音声

音声は `gpt-realtime-2.1-mini` 固定とする。

- 雑談、聞き返し、手元に同期済みの予定・締切・状態は Realtime が直接応答する。
- 次の予定は `get_schedule` などの手元ツールを呼ぶだけであり、agent へ委譲しない。
- 研究・大学・仕事の中身を調べる必要があれば、Realtime は待つ旨を短く伝え、選択済み provider の対応 agent へ委譲して結果を音声で返す。
- 実行依頼は内容を復唱し、利用者の確認後だけ Slack の Kei Agent へ送る。
- Realtime API の支出は Claude/Codex のサブスク quota と分けて警告・上限管理する。

## 評価と本採用

レシピは各 provider・ユースケースごとに代表入力で評価する。本採用には以下すべてを満たすことを求める。

1. 必須事実に誤り・見落としがない。
2. 根拠となる資料名と該当箇所を示す。
3. 不明点を推測で埋めない。
4. 必要な tool を正しく選択する。
5. 所要時間と quota 消費が許容範囲に収まる。

不合格なら、該当 provider の該当ユースケースだけを上位モデルまたは effort に変える。別 provider の結果を流用しない。

## 現行実装との差分

- 現在の model recipe は provider ごとではなく全体で3個だけで、agent の provider 切替後にユースケース別 provider レシピを解決できない。
- allowlist がなく、`gpt-5.6-luna` と `gpt-5.6-terra` が設定に残る。
- router は独立した profile を持たず、研究 profile を流用する。
- Claude の通常 CLI 経路は選択した effort を実行引数へ渡していない。
- Claude/Codex の connector・権限は対称でない。
- voice は low effort と Codex 固定委譲を前提にしている。

実装時は allowlist、provider capability preflight、provider 別レシピと effort adapter、router/voice の独立設定を一つのモデル方針 migration として扱う。時間カードの実装とは別の変更である。
