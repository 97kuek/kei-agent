---
name: managing-wandb
description: Use when the research agent needs to inspect or record Weights & Biases runs, metrics, artifacts, or sweeps.
---

# W&Bを研究記録に使う

既定は読み取り。run、summary、config、artifact metadataを確認し、詳細ログやraw datasetはローカルに残す。`run.log`、artifact upload、sweep開始、削除は明示依頼がある場合だけ行う。MCPが利用可能なら研究エージェントだけに限定する。接続や権限が不明なら、`wandb` CLI/SDKやローカル認証情報を勝手に探さず未接続として扱う。Notionへはrun URL、commit、主要指標、結論だけを投影する。
