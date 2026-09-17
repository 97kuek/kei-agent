# Codex App 側の使い方

`docs/plan.md` 第6章のステップ11。Codex App は「考える場所」で、読むだけにする。実行は Ezra だけが行う。

## 1. `~/research/` を開く

Codex App で `~/research/` をプロジェクトとして開く。これで次のものを直接読める。

| 読むもの | 場所 |
|---|---|
| テーマの前提 | `~/research/<theme>/CLAUDE.md` |
| 図や集計結果 | `~/research/<theme>/outputs/` |
| ジョブのログ | `~/research/<theme>/logs/job-<ID>.log` |
| 文献調査の結果 | `~/research/<theme>/papers/` |
| スレッドの経緯（依頼と Ezra の返答） | `~/research/<theme>/.ezra/threads/<thread_ts>.md` |
| ジョブの状態 | `~/research/<theme>/.ezra/jobs/<ID>.json` |
| Daily | `~/research/_overview/daily/<日付>.md` |
| 振り返りの材料 | `~/research/_overview/reviews/<日付>.md` |

Codex に最初に伝えておくとよいこと（プロジェクトの指示として `~/research/AGENTS.md` に置いてもよい）:

```markdown
- ここは研究の作業用ディレクトリ。実行は Slack Bot「Ezra」が行うので、ここではファイルを変更しない
- 経緯は各テーマの `.ezra/threads/*.md`、結果は `outputs/`、前提は `CLAUDE.md` を読む
- 追加の実験や調査が必要になったら、Ezra への依頼文を下のテンプレートで作る
```

## 2. Slack のスレッドを読む（任意）

`.ezra/threads/` で経緯は読めるので、必須ではない。Slack の画面上のやり取りも読ませたいときに使う。

- ChatGPT / Codex の Slack コネクタが使えるプランなら、それをつなぐ
- Slack 公式の MCP サーバー（`https://mcp.slack.com/mcp`）は、2026年9月時点で Codex への組み込みが公式には提供されていない。使うときは、Slack と Codex の最新のドキュメントで対応状況を確認する
- `~/.codex/config.toml` は dotfiles で管理しているので、設定を足すときは `~/dotfiles` 側を編集する

## 3. 依頼文のテンプレート

Codex で議論して決めた作業は、次の形で Ezra に送る。最初は Codex が作った文を自分で Slack に貼る。形が固まったら、Codex から直接投稿する運用に切り替える。

```markdown
@Ezra
## やりたいこと
（例: 条件Bでも counting の精度を測り、条件Aと比べる図を作る）

## 背景と判断の経緯
（例: 条件Aで 82% だった。Bで下がるなら、質問を先に見せることが効いていると言える）

## 条件
（例: モデル、データ、試行数。CSV を添付する場合はその説明）

## 返してほしいもの
（例: 条件ごとの精度の表と、95%区間つきの図。所要が長ければジョブにする）
```

## 4. 振り返り（毎晩 21:00）

Ezra が `#research-overview` に「振り返りの材料」を投稿し、`~/research/_overview/reviews/<日付>.md` に同じ内容と問いを書く。

1. Codex App でそのファイルを開き、音声チャットで振り返る
2. 結論を、ファイルの「Codex での振り返り」に書くか、Slack のスレッドに貼る（Ezra がファイルに追記する）
3. 翌朝 08:00 の Daily に反映される

## 5. 音声での議論

Codex App の音声チャット（**Start new voice chat**）で議論する。Plus 以上のプランが必要で、音声チャットは同時に1つまで。
結論が出たら、「Ezra への依頼文をテンプレートで作って」と頼み、テキストで確認してから送る。
