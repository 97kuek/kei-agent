# 知識（`knowledge`）

Knowledge は Dot がクラウドで受け持つ役割。Mac の AI・A2A プロセスには依頼しない。

| 処理 | 接続先と動作 |
|---|---|
| 興味・知識の確認 | Notion の知識ホーム（「収集」ページと「Knowledge」）を読む |
| 記事・論文の検索 | Web で Zenn・Qiita・arXiv などを探し、元の資料を読む |
| 質問・要約 | Dot が資料を読んで、出典を添えて答える |
| 保存・更新 | 知識ホームの「Knowledge」に直接書く。記事は Source（URL）で照合し、重複を作らない |
| 毎朝の読みもの | Dot の予定から `#4-knowledge` に1記事1親投稿。3記事なら3投稿 |
| 先行研究の新着 | Dot の予定から、各テーマのページの「先行研究」DB に新着を入れ、要約を `#0-overview` へ |

Mac が閉じていても処理できる。`run(workspace="knowledge")` は使わない。接続できないプラグインがあれば未確認と伝える。

## 知識ホーム

共通ホームと同じくワークスペースの直下のページ。中に置くのは「収集」と「Knowledge」の2つだけ。共通ホームの「リンク」から開ける。

- ページは Notion の画面で作り、Kei Agent の接続に共有する。ページ ID は `agents.csv` の knowledge の行の `notion` 列に書く
- 「収集」と「Knowledge」を作るのは `uv run kei-agent-hub-setup --apply`。同じ名前のものがあれば作らずに使い、Knowledge の表の列を下の順にそろえる。共通ホームに「収集」が残っていれば、移すまで止まる

### Knowledge

| 列 | 種類 | 中身 |
|---|---|---|
| Title | タイトル | 記事の題、または学びを1行で |
| Type | 選択 | Article / Learning / Advice / Insight |
| Summary | 文 | 記事なら要約2文、学びなら学んだことを1〜2文 |
| Source | 文 | 記事なら URL（照合キー。同じ URL の行があれば作らない）、学びなら誰から・どこで |
| Status | 選択 | Unread / Read。Article にだけ付け、学びは空 |

- 学びは振り返りのスレッドの会話から入る（学び→Learning、助言→Advice、気づき→Insight）。本文は「場面・学んだこと・次にどう使うか」と、最後に学びが出た Slack のスレッドのリンク
- 日付は持たない。Notion のページの作成日時を使う

## 定期実行と保存

[プロンプト一覧](../prompts/README.md) の読みもの・先行研究の指示を使う。毎朝の記事選びは、収集の興味と、最近の Article の Title と Summary から好みを読み取る。記事の各スレッドで「詳しく」「要約して」「保存して」と頼める。「保存して」は Knowledge に Title・Type=Article・Summary・Source=URL・Status=Unread で1行入れる。

「収集」ページは、見出し「興味」「情報源」の下に箇条書きで書く。

```markdown
## 興味
- LLM エージェント: agent、tool use、MCP
## 情報源
- Zenn: llm、python
- Qiita: 機械学習
- arXiv: cs.AI
```

## 旧配信記事の保存

`modules/knowledge/` は旧記事の保存処理だけを保持する。MCP `reading` で取得できる旧記事は `save_reading` で Knowledge に Article（Status=Unread）として保存でき、保存に失敗したものも再試行できる。解除は同じ URL の行をゴミ箱に入れる。

新しい記事の検索・要約・配信はこのモジュールを通さない。ローカルの `reading`・`literature` 定期実行と AI プロセスはない。
