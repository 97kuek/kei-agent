---
name: managing-wandb
description: Use when the research agent needs to inspect or record Weights & Biases runs, metrics, artifacts, or sweeps.
---

# W&Bを研究記録に使う

W&Bは実験のメトリクス、run、artifactの索引として使い、詳細ログや個人情報はローカルの研究ディレクトリに残す。

- 既定は読み取り（run一覧、summary、config、artifact metadata）
- `run.log`、artifact upload、sweep開始、削除は依頼者が明示したときだけ
- MCPが利用可能なら研究エージェントのMCP設定だけに追加し、course/workには見せない
- MCPの接続や権限が不明なときは、`wandb` CLI/SDKのローカル設定を勝手に探さず、未接続として説明する
- 外部に送るデータはプロジェクト名、run名、数値メトリクスなど必要最小限にし、学生情報・秘密情報・raw datasetは送らない

実験をNotionへ記録するときは、W&Bのrun URL、commit、主要指標、結論だけを研究ホームの研究ログへ投影する。完全なstdoutやcheckpointはNotionに貼らない。
