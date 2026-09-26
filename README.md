# Kei Agent

> Slack で頼んだ用事を、担当のエージェント（研究・大学・仕事・知識）に振り分けて進めるパーソナルアシスタント

## 何ができるか

- **研究**（`#10_<テーマ>`）… テーマごとの作業場（`~/research/<テーマ>/`）でコードを書き、実験を回し、論文を探し、研究ホーム（Notion）に残す
- **大学**（`#20_course`）… Moodle の締切、Box の学部要項・過去問、Notion の授業ホーム（課題・成績・単位）を扱う
- **仕事**（`#30_work`）… 会社の Outlook・メール・SharePoint・Teams を読んで答える（送信や予定の変更はしない）
- **知識**（`#40_knowledge`）… 興味のある技術記事と研究テーマの論文の新着を毎朝選んで要約し、その質問に答える
- **定期実行** … 07:00 先行研究の新着、08:00 今日の予定と Daily、21:00 Retro & Planning、00:00 🌙 を付けた Task
- **自己改善**（`#00_kei-agent`）… 要望から Kei Agent 自身のコードを直し、承認されたものだけをテストして取り込む
- **声** … 机の上で声で相談できる（OpenAI Realtime API。既定は切）

## 構成

```text
Slack（個人ワークスペース、Socket Mode）
  │
  ▼
Kei Agent 本体（オーケストレーター, src/kei_agent/）   :8786  声からの問い合わせ口
  Slack の受け口・振り分け・柵・Notion・定期実行・自己改善
  │  A2A（127.0.0.1、共有トークン）。エージェントを呼べるのは本体だけ
  ├─ 大学エージェント   :8787  Moodle / Box / 授業ホーム / Toggl
  ├─ 研究エージェント   :8788  テーマの作業場で AI を動かす / pueue ジョブ
  ├─ 仕事エージェント   :8789  Microsoft 365（読むだけ）
  ├─ 知識エージェント   :8792  読みもの・論文の新着（Notion・Slack は持たない）
  └─ 声のレイヤ         :8790  Realtime API / マイク / スピーカー
  Notion ゲートウェイ   :8791  Notion に届く唯一の口（使う側ごとに届くホームを絞る）
```

各エージェントは Claude Code CLI か Codex CLI のどちらかで動く（App Home で agent ごとに選ぶ）。どちらでも、
使える道具と届く範囲は同じ制限の表（`src/kei_agent/agent_policy.py`）で決まる。
すべて同じ Mac の launchd で常駐する。

## ドキュメント

| 読みたいこと | 場所 |
|---|---|
| Slack での使い方（チャンネル、合図、時間記録、App Home、声） | [`docs/using.md`](docs/using.md) |
| いまの仕組み（プロセス、provider とモデル、出力、柵、Notion、定期実行、声） | [`docs/architecture.md`](docs/architecture.md) |
| 入れ方と運用（Slack App、秘密情報、launchd、Notion の準備、困ったとき） | [`deploy/README.md`](deploy/README.md) |
| 開発の手順、テスト、書き方 | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## ライセンス

MIT（`LICENSE`）
