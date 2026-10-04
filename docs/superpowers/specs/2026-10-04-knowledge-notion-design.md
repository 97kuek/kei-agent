# 知識の Notion の作り直し

## 目的

- 知識（記事・学び・助言・気づき）を、毎日の動きの記録（共通ホーム）から分けて、1か所で探せるようにする
- 読みものと学びのノートの2つの DB を1つにし、列を減らす
- 研究・授業の Notion と同じ書き方（列名と選択肢は英語、列の順番を決める）にそろえる

## 決めたこと

### 知識ホーム

- 新しいページ「知識ホーム」を、共通ホーム（Keitaro Ueki）と同じ並び（ワークスペースの直下）に作る
- 中に置くのは次の2つだけ
  1. **収集**（ページ）: 興味とキーワード、記事を集める情報源。共通ホームから移す。書き方（「名前: キーワード、…」と、情報源の行）は今のまま
  2. **Knowledge**（DB）
- 共通ホームの「リンク」には、知識ホームへのリンクを置く

### Knowledge（DB）

| 列 | 種類 | 中身 |
|---|---|---|
| Title | タイトル | 記事の題、または学びを1行で |
| Type | 選択 | Article / Learning / Advice / Insight |
| Summary | 文 | 記事なら要約2文、学びなら学んだことを1〜2文 |
| Source | 文 | 記事なら URL（照合キー。同じ URL の行があれば作らない）、学びなら誰から・どこで |
| Status | 選択 | Unread / Read。Article にだけ付け、学びは空 |

- 列はこの順に作り、表の表示もこの順にそろえる
- 学びのページの本文は、今と同じく「場面・学んだこと・次にどう使うか」。最後に、学びが出た Slack のスレッドのリンクを書く
- 日付は持たない。Notion のページの作成日時を使う
- 毎朝の記事選びは、最近の Article の Title と Summary から好みを読み取る。興味の分け方は収集のページを使う

### なくすもの

- 読みもの（DB）と学びのノート（DB）
- 写さずに捨てる列: 読みものの出どころ・興味・日付、学びのノートの分野・日付（それ以外の列は Knowledge に写す。移し方を参照）

## 合わせて直すもの

### Kei Agent

- `src/kei_agent/storage/notion_hub.py`・`notion_hub_setup.py`: 共通ホームを作るコードから、読みもの・学びのノート・収集を外す。知識ホーム（収集のページと Knowledge の DB）を作り、読み書きする処理を足す。収集を読む処理（`collect`）は知識ホームの収集を読む
- `modules/daily/`: 振り返りの学びを、Knowledge に Type（学び→Learning、助言→Advice、気づき→Insight）で入れる。日別記録のレトプラに題とリンクを足す処理は今のまま
- `modules/knowledge/`: 旧ローカル配信分の保存（`save_reading`）を、Knowledge に Type=Article・Source=URL・Status=Unread で入れる形にする。解除は同じ URL の行をゴミ箱に入れる
- 設定: 知識ホームのページ ID の置き場所（共通ホームと同じく `agents.csv` の行で持つ）
- `tests/` と `src/kei_agent/testing/fakes.py` の該当する偽物

### Dot（`docs/prompts/`）

- 読みもの: 収集のページは知識ホームのものを読む。最近の好みは Knowledge の Type=Article の行から読む
- 振り返り: 学びの返事を受けたら、Knowledge に1件1ページで入れる（継続指示側）
- 継続指示: 記事の「保存して」は Knowledge に Title・Type=Article・Summary・Source=URL・Status=Unread で1行（同じ Source の行があれば作らない）。学びは Title・Type・Summary・Source（誰から・どこで）で1行、本文に場面・学んだこと・次にどう使うかと Slack のリンク
- 定期実行の Notion 接続先の一覧（共通の決まりの中の data source の ID）を、Knowledge に差し替える

### 文書

- `docs/agents/knowledge-agent.md`・`docs/using.md` と、共通ホームの作りを書いている箇所

## 移し方

1. 知識ホームのページを作り、収集のページを移す
2. Knowledge の DB を作る
3. 読みものの行を Type=Article で移す（名前→Title、要約→Summary、URL→Source、状態: 気になる→Unread・読んだ→Read）。出どころと興味は捨てる
4. 学びのノートの行を移す（名前→Title、種類: 学び→Learning・助言→Advice・気づき→Insight、本文の「学んだこと」→Summary、出典→Source、本文はそのまま写し、Slack のリンクを本文の最後に足す）。分野は捨てる
5. 行の数と中身が古い DB と合っているかを確かめる
6. Dot のプロンプトとリポジトリのコードを切り替える
7. 利用者に確認してから、読みものと学びのノートの DB を消す（ゴミ箱に入り、30日間は戻せる）

## 確かめ方

- `uv run python -m pytest` と `uvx ruff check .`
- 記事のスレッドで「保存して」と言うと、Knowledge に Article で1行入り、もう一度言っても2行にならないこと
- 振り返りの学びの返事が、Knowledge に Learning・Advice・Insight のどれかで入ること
- 翌朝の読みものが、知識ホームの収集を読んで選ばれること

## 今回は扱わないもの

- 共通ホームに残る DB（予定カレンダー・日別記録・時間記録・Dot Work Log）の見直し
- 研究と授業の Notion（別の設計書で扱う）
