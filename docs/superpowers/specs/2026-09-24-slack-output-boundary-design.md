# Slack 出力境界の統一設計

## 目的

Claude と Codex のどちらを provider に選んでも、Kei Agent が Slack に出す文は利用者向けの結論だけにする。
model が出した作業手順、tool / skill 名、ローカルパス、環境エラー、途中の独り言を最終メッセージへ流さない。
Daily は Slack の Markdown で太字になる見出しを使い、Kei Agent の口調に統一する。

## 観測した問題

- Retro & Planning だけが投稿前に厳格な構造検証をしており、Daily と通常会話は `RunResult.text` をそのまま投稿している。
- `ThreadUI.text` と A2A の `on_text` は provider の途中テキストをそのまま status に渡す。
- `publish()`、大学・仕事の free-form reply、定期投稿は別々に Slack へ出しており、provider ごとの振る舞い差を吸収する共通境界がない。
- runner / connector の失敗理由をそのまま出す経路があり、内部の exception 名、パス、設定値を含みうる。

## 原則

1. **内部経過と最終回答を分ける。** provider の途中 text は Slack 本文にしない。status は固定の利用者向け短文だけにする。
2. **Slack 本文は種別を持つ。** Daily / Retro のような定期投稿は厳格な構造契約、自由回答は会話契約を通す。
3. **不正な出力を推測して直さない。** 契約違反の本文は破棄し、安全な一文を返す。部分的な切り貼りで思考や誤情報を残さない。
4. **provider に依存しない。** Claude、Codex、A2A のどの経路でも、Slack の直前に同じ renderer を通す。
5. **利用者向けの失敗を出す。** 内部エラーはログへ残し、Slack には接続・上限・一時失敗など必要な状態だけを Kei Agent の口調で出す。

## 出力契約

### 通常会話

通常会話の model prompt は最終回答だけを `<<kei-agent-final>>` と `<<kei-agent-final-end>>` の間に返す。renderer はその区画だけを採用する。

採用する文は次を満たす。

- 日本語で、Kei Agent の一人称は「僕」。結論から短く返す。
- 作業予定・逐次手順・tool / skill / CLI / model 名・ローカルパス・file URI・秘密情報らしき値を含めない。
- 自動制御行（`❓ 確認:`、`🧵 区切り:`、`🔒 接続:`）は final 区画の末尾にだけ許可し、既存の制御処理へ渡す。

marker が無い、複数ある、禁止内容を含む場合は本文を投稿せず、`⚠️ 返答を利用者向けの形に整えられなかったよ。もう一度頼んでね。` を出す。内部の原文は Slack に保存しない。

### Daily

Daily は final marker 内で次の4 section だけを許可する。

1. `**今日のタスク**`
2. `**夜間処理の結果**`
3. `**確認待ち・期日・止まっているテーマ・返事待ち**`
4. `**今日考えるとよい問い**`

各 section は空にせず、余分な見出し、内部情報、作業実況を拒否する。`*見出し*` は使わない。model へは「僕」の口調で、利用者が判断・行動できる事実だけを返すよう指示する。検証失敗時は Daily の本文を投稿・Notion 保存せず、安全な失敗通知だけを残す。

### Retro & Planning

既存の3 block 契約（今日の成果、未完了タスク、夜間質問）を同じ module に移す。見出しは Slack で太字となる `**...**` に統一する。Retro の本文と footer はローカル path や Codex App の案内を含まない。

### 進捗とエラー

`ThreadUI` は model が出力した text を status に使わない。tool activity もコマンドやファイル名を出さず、`調べている…`、`作業している…`、`まとめている…` の固定 set に正規化する。

Slack の error renderer は exception の文字列を渡されても公開しない。quota、接続、タイムアウト、設定不足、その他の失敗を短い固定文へ分類し、詳細はアプリのログだけに残す。

## 実装境界

`src/kei_agent/response_output.py` を唯一の renderer とする。

- `finalize_conversation(text)`: final marker を抽出し、通常会話契約を検証する。
- `validate_daily(text)`: Daily の4 section 契約を検証する。
- `validate_review_reply(text)`: Retro の契約をここへ移管または互換 re-export する。
- `safe_failure(...)`: Slack に出せる固定失敗文を返す。

`Assistant._reply()`、`Assistant.publish()`、大学・仕事の `ask`、定期処理は renderer を通してから `post()` / `ThreadUI.finish()` を呼ぶ。run / A2A / connector は raw output を返すだけで Slack 表示を判断しない。

既存 session は旧 prompt で始まっているため、`rules_update_prompt()` と各 agent prompt に final marker の決まりを入れる。session を捨てず、次の request から更新規則を先頭に渡す。

## Moodle 秋冬科目

Moodle ICS は読み取り専用で取得する。calendar event から科目名を重複なく抽出し、授業ホームの `授業` DB と比較する。

- 既存の授業に一致する event は、既存の課題同期と同じ経路で取り込める。
- calendar にしかない科目は、科目名の候補として報告する。
- ICS だけでは曜日・時限・履修状態を確定できないため、新規の `授業` 行、relation、課題を自動作成しない。

## テスト

1. Claude / Codex / A2A の raw text が final marker 外に作業実況を含んでも Slack 本文に出ない。
2. Daily の4太字見出し、順序、口調、禁止語、空 section を検証する。
3. Retro の既存契約と URL / absolute / relative path 拒否を維持する。
4. status と failure に raw model text、command、path、exception が出ない。
5. 既存の handoff、awaiting、domain connection marker が final marker 内で機能する。
6. Moodle の科目候補抽出と既存 DB 照合は read-only である。

## 非目標

- model 出力を追加の model で言い換えること（費用と新たな漏洩経路を増やすため）。
- ICS だけを根拠に、曜日・時限・履修状態を推測して Notion の科目台帳を変更すること。
- Slack の過去投稿を編集・削除すること。
