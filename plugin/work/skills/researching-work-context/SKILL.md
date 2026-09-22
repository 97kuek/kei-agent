---
name: researching-work-context
description: Use whenメール、Teams、SharePoint を横断して、ある件の経緯や相手の言い分を調べるとき。
---

# 会社のことを根拠つきで調べる

持っている道具は**読み取り専用**。送信も作成も更新もできない。

## 探し方

1. `outlook_email_search` と `chat_message_search` を、件名・相手・キーワードで引く
2. 資料は `sharepoint_search`、フォルダから辿るなら `sharepoint_folder_search`
3. Teams のやりとりは `teams_list_channel_messages`、相手は `search_people`
4. 見つけたものの本文は `read_resource` で読む

同じ件でも呼び名が揺れる（案件名・社名・略称）。2〜3通り試してから「無い」と言う。

## 返し方

- **差出人・日付・件名**を必ず添える。原典の URL も出す
- 短いものはそのまま出す。長いものは要点をまとめ、判断に関わるところだけを `>` で引く
- 日付と相手は取り違えると困るので、原典を確かめてから書く
- 件数が多いときは、件数と直近の3件だけを出して「もっと見る？」と聞く
