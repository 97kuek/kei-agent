---
name: job
description: 数分以上かかる実験や分析を、Kei Agent のジョブ（pueue）としてバックグラウンドで実行する。長い処理をその場で実行しそうになったとき、ジョブの状態を確認したいとき、取り消したいときに使う。
---

# Kei Agent のジョブ

数分以上かかる処理は、その場で実行せずにジョブにする。ジョブは Claude Code の外で走り、終わると Kei Agent がこのスレッドの会話を再開する。

## 手順

1. 実行するスクリプトを、カレントディレクトリの中に作る（例: `scripts/sweep.py`）
   - 結果は `outputs/` に、途中経過は標準出力に書く（標準出力と標準エラーは `logs/job-<ID>.log` に保存される）
   - `.py` は、カレントディレクトリに `pyproject.toml` があれば `uv run python`、なければ `python3` で実行される。`.sh` は `bash` で実行される
   - 最初に小さな条件で数秒だけ試し、動くことを確かめてから投入する
2. 投入する:

   ```bash
   python3 "$KEI_AGENT_PLUGIN_DIR/skills/job/scripts/kei_agent_job.py" submit --name "<短い名前>" \
     --expect outputs/sweep.csv --expect outputs/sweep.png scripts/sweep.py -- --arg1 value
   ```

   - `--expect` には、そのジョブでできるはずのファイルを書く（複数回書ける）。終わったときに Kei Agent が
     有無と中身の空でないことを確かめて、できていなければ報告に添える。終了コードが 0 でも中身が空のことがあるため

3. 投入を依頼したこと、何を走らせたか、終わったら報告することを返答に書き、その回の作業を終える。終わるまで待たない

## 状態の確認と取り消し

```bash
python3 "$KEI_AGENT_PLUGIN_DIR/skills/job/scripts/kei_agent_job.py" status        # すべて
python3 "$KEI_AGENT_PLUGIN_DIR/skills/job/scripts/kei_agent_job.py" status 12     # ジョブ12
python3 "$KEI_AGENT_PLUGIN_DIR/skills/job/scripts/kei_agent_job.py" cancel 12
```

## 制限

- スクリプトはカレントディレクトリの中にあるものだけ。絶対パスや `..` は使えない
- ジョブは sandbox の外で動く。カレントディレクトリの外に書き込むスクリプトや、ネットワークから取得したコードをそのまま実行するスクリプトは作らない

## ジョブが終わって会話が再開されたとき

1. `logs/job-<ID>.log` の末尾と `outputs/` を確認する。`--expect` の照合結果が自動メッセージに書いてあるので、
   「できていない」と書かれていたら、成功と表示されていても失敗として扱う
2. 失敗していたら原因を調べ、直せるなら直して再投入するか、判断が要るなら依頼者に聞く
3. 成功していたら、集計して図を `outputs/` に保存し、結果を報告する
