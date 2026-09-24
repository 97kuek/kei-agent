# 用途別モデル方針の実装計画

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Claude と Codex を同等に選べる agent 単位の provider 選択と、用途ごとの provider 別モデル・effort レシピを実装する。旧 GPT-5.6 系を完全に除外し、Realtime 音声と通常 agent を安全に分離する。

**Architecture:** App Home と SQLite は各実行役の provider だけを保持する。実行直前に `use_case` と provider から不変なレシピを解決し、provider/model/effort/capability を一つの `ResolvedModel` として runner へ渡す。skill は呼び出した agent の `use_case` を指定するだけで、自由文は軽量 classifier が候補を選ぶ。未選択 provider・未接続 capability・利用上限は、別 provider への再試行をせず明示的に失敗させる。

**Tech Stack:** Python 3.12、SQLite、Slack Bolt/App Home、Codex CLI、Claude Code CLI、OpenAI Realtime API、pytest/uv。

**Scope boundary:** この計画はモデル方針の実装だけを対象にする。`docs/superpowers/plans/2026-09-23-slack-toggl-time-cards.md` の Slack/Toggl 時間記録、A2A 成果物、Notion 連携の機能追加は実装しない。

## 1. 契約を先に固定する

**Files:**

- Modify: `src/kei_agent/config.py`
- Modify: `config.toml`
- Modify: `tests/test_themes.py`
- Create: `tests/test_model_policy.py`

1. `ModelRecipe` を「任意名の汎用レシピ」ではなく、`use_case`、provider、model、effort を持つ provider 別レシピに置き換える。解決結果には provider、model、effort、use case、必要 capability を持たせる。
2. allowlist をコードの唯一の定義にする。Codex は `gpt-6-luna`、`gpt-6-sol`、`gpt-6-astra`、Claude は `claude-haiku-4-5`、`claude-sonnet-5`、`claude-opus-5`、`claude-fable-5` だけを許す。その他（特に `gpt-5.6-*`）は設定読み込み時に拒否する。
3. effort を provider ごとに検証する。Codex は `low`、`medium`、`high`、`xhigh` を許す。Claude は CLI が受け付ける値だけを明示許可し、モデルごとに利用不可な値があれば設定検証で止める。名称が同じ effort でも provider 間の品質同等性を仮定しない。
4. 現行の `default_recipe`、workspace に載せる `model` / `reasoning_effort`、App Home のモデル・effort override を廃止する。用途に対するレシピを変えられる設定口は作らない。
5. `AgentProfile.provider` は `"" | "codex" | "claude"` とし、未選択を正当な初期状態にする。対象は `research`、`course`、`work`、`router`、`self_fix`。後二者は skill を持たない実行役なので、`AGENT_PLUGINS` と runner profile の集合を分離する。
6. `config.toml` を allowlist のモデル名と全 use case の provider 別レシピへ移行する。ただし各実行役の provider は空にして、起動後に明示選択されるまで実行不可にする。Realtime のモデルは通常 recipe に混ぜず、専用設定定数として扱う。

Tests first:

- provider 未選択、未知の provider、allowlist 外モデル、provider/モデル不一致、unsupported effort を設定読み込みで拒否する。
- 全 use case が Claude/Codex の両方で一意に解決できること、Astra/Fable が通常レシピに含まれないことを確認する。
- `gpt-5.6-luna` / `gpt-5.6-terra` を含む旧設定が明示的な移行エラーになることを確認する。

## 2. 用途カタログと解決器を実装する

**Files:**

- Create: `src/kei_agent/model_policy.py`
- Modify: `src/kei_agent/config.py`
- Modify: `src/kei_agent/settings.py`
- Modify: `tests/test_model_policy.py`
- Modify: `tests/test_settings.py`

1. `UseCase` enum と `resolve_model(config, store, actor, use_case)` を作る。返却する `ResolvedModel` は raw profile ではなく、必ず allowlist 済みの model/effort を持つ。
2. 採用済みの通常レシピを定数と config に写す。識別子は次を使う。

| Actor | Use case | Codex | Claude |
| --- | --- | --- | --- |
| router | `routing` | Luna / low | Haiku 4.5 / thinking disabled |
| research | `research_extract` | Luna / low | Haiku 4.5 / thinking disabled |
| research | `research_screen` | Luna / medium | Haiku 4.5 / thinking disabled |
| research | `research_compare` | Sol / medium | Sonnet / medium |
| research | `research_execute` | Sol / high | Sonnet / high |
| research | `research_design` | Sol / xhigh | Opus / high |
| course | `course_explain` | Luna / medium | Sonnet / medium |
| course | `course_requirements` | Luna / high | Sonnet / high |
| course | `course_compare` | Sol / medium | Sonnet / high |
| course | `course_degree_plan` | Sol / xhigh | Opus / high |
| work | `work_single_source` | Luna / medium | Sonnet / medium |
| work | `work_cross_source` | Sol / medium | Sonnet / high |
| work | `work_decide` | Sol / high | Opus / high |
| router | `overview_daily` | Luna / medium | Sonnet / medium |
| router | `overview_plan` | Sol / high | Opus / high |
| self_fix | `self_fix_design` | Sol / xhigh | Opus / high |
| self_fix | `self_fix_implementation` | Sol / high | Sonnet / high |
| self_fix | `self_fix_review` | Sol / medium | Sonnet / high |

3. Astra と Fable は `manual_astra` / `manual_fable` の明示指定にだけ解決可能な例外レシピとする。通常の classifier、skill、schedule、retry は選択できない。リクエスト本文に明示的な label があり、依頼者本人の Slack ID である場合だけ有効にする。
4. `settings.set_agent_provider` は provider だけを保存するよう整理し、旧 model/effort キーを読みも書きもしない。`selected_provider` と `require_provider` を導入し、未選択なら「`<actor>` の provider を App Home で選んでください」と返す。
5. 既存 DB に model/effort override があっても無視・消去する一回限りの migration を用意する。provider 値は保存済みなら維持するが、旧世代モデルを provider 選択の根拠にしない。

Tests first:

- provider を切り替えると、同じ `use_case` が相手 provider の別レシピに解決される。
- provider 未選択、存在しない use case、manual exception の自動選択を失敗させる。
- 旧 DB の model/effort override がレシピを上書きしないことを確認する。

## 3. App Home を provider 専用の設定画面にする

**Files:**

- Modify: `src/kei_agent/home.py`
- Modify: `src/kei_agent/settings_actions.py`
- Modify: `src/kei_agent/settings.py`
- Modify: `tests/test_home.py`
- Modify: `tests/test_settings.py`

1. 現在の研究・授業・仕事の「provider / model / effort」3 selector を provider selector のみにする。表示は選択済みの provider か「未選択」とする。
2. 追加で「ルーティング・全体計画」「自己改善」の provider selector を表示する。前者は route と Daily/Retro の横断的な用途を同じ provider で担う。用途ごとの model/effort は表示しないが、選んだ provider で実行される代表レシピを説明文として示す。
3. 未選択の actor は実行ボタンを無効化しない（通知を見失わないため）が、実行要求では明確な失敗メッセージを返す。どちらかを既定選択したように見える UI を置かない。
4. 旧 `kei_agent_home_model:*` と `kei_agent_home_effort:*` action は受け付けず、再送・古い Slack view では安全に無視して Home を再描画する。

Tests first:

- 5 actor の provider 選択が保存され、他 actor に波及しないことを確認する。
- Home JSON に model/effort selector がなく、未選択が明瞭に表示されることを確認する。
- 旧 action が model を保存しないことを確認する。

## 4. runner と connector の provider/capability 境界を一本化する

**Files:**

- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent/agent_policy.py`
- Modify: `src/kei_agent_a2a/claude.py`
- Modify: `src/kei_agent/codex_app_server.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_agent_claude.py`

1. `runner.run_claude` という provider を隠す名前を `run_model` に改め、`ResolvedModel` を必須引数にする。Codex/Claude の各 command builder は、解決済み model/effort 以外を受け取らない。
2. Claude CLI の `--model` と `--effort` を command に明示する。CLI の実行時 capability probe で選択モデル・effort が使えない場合は、provider/model/use case と失敗理由を返して止める。
3. Codex は `--config model_reasoning_effort=…`、app server の `reasoningEffort` に解決済み effort を渡す。Astra/Fable への自動 retry は実装しない。
4. capability preflight を `agent_policy` にまとめる。Codex は用途に必要な MCP/connector と sandbox 権限、Claude は接続済み connector と利用可能 CLI/model を確認する。provider が必要 capability を持たなければ、選択した provider のまま明示エラーにする。
5. `ask_connector`、A2A 研究 executor、通常 Slack 実行の全経路が同じ preflight と解決器を通るようにする。接続失敗、モデル上限、tool 不在のときに別 provider を呼ばないことをテストする。

Tests first:

- provider ごとの command に許可済み model と正しい provider 固有 effort が一度だけ載ることを確認する。
- connector が不足すると Claude/Codex の相互 fallback をせず、preflight error になることを確認する。
- provider 選択と capability を満たすと Codex app server / Claude CLI の正しい経路を選ぶことを確認する。

## 5. skill・自由文・router を用途指定に移行する

**Files:**

- Modify: `src/kei_agent/research.py`
- Modify: `src/kei_agent_research/executor.py`
- Modify: `src/kei_agent/router.py`
- Modify: `src/kei_agent/assistant.py`
- Modify: `src/kei_agent/schedule.py`
- Modify: `tests/test_research_agent.py`
- Modify: `tests/test_assistant.py`
- Modify: `tests/test_schedule.py`

1. skill 起点の request payload を旧 `model_recipe` ではなく `use_case` にする。固定 skill は次の対応を直接指定する。
   - 文献の固定抽出・タグ・重複検出は `research_extract`、一次スクリーニングは `research_screen`。
   - 文書比較・結果分析は `research_compare`、実験コード・データ・通常論文は `research_execute`、仮説・手法選定・厳密レビューは `research_design`。
   - GPA/履修充足/締切同期など決定的処理は model を呼ばない。
2. 自由文 `ask` には選択 provider の軽量 classifier を一回だけ使う。Codex は Luna/low、Claude は Haiku 4.5/thinking disabled とし、JSON で `use_case` と confidence を返させる。parse error、unknown、低 confidence はその actor の安全な通常レシピに落とし、強いモデルで再分類しない。
3. 利用者が `[[course-requirements]]` のような label を書いた場合は classifier より優先し、実行 prompt から label を取り除く。許可する label は UseCase enum の通常ケースだけで、manual exception は明示 owner-only path を経由する。
4. router は `router` actor の provider を必須とし、`routing` 解決結果だけで分類する。hard-coded で未使用の `MODEL = "haiku"` を消す。JSON が不正・低 confidence なら `ask` に戻し、強い再試行・別 provider retry はしない。
5. scheduler はモデルを呼ぶ処理だけに use case を渡す。morning timeline と maintenance は決定的処理のまま、literature は `research_compare` を research actor で、Daily/Retro は `overview_daily` / `overview_plan` を router actor で解決する。night task は元 request の明示 use case または research の自由文分類を引き継ぐ。
6. `SelfFix.improve` は会話中の設計に `self_fix_design`、worktree 実装に `self_fix_implementation`、差分レビューに `self_fix_review` を渡す。既存の worktree、guard、テスト、ユーザー承認、merge/push の柵は変更しない。

Tests first:

- 明示 label が classifier より優先され、最終 prompt に残らないことを確認する。
- classifier 不明/壊れた JSON が既定の安全ケースになり、強化 retry されないことを確認する。
- router が自身の provider profile を使い、未選択なら実行しないことを確認する。
- night task が「Opus 固定」にならず元 use case を継承すること、決定的な授業計算がモデルを起動しないことを確認する。

## 6. 音声を `gpt-realtime-2.1-mini` と agent handoff に分離する

**Files:**

- Modify: `src/kei_agent_voice/live.py`
- Modify: `src/kei_agent_voice/tools.py`
- Replace: `src/kei_agent_voice/think.py`
- Modify: `src/kei_agent_voice/session.py`
- Modify: `src/kei_agent_voice/app.py`
- Modify: `tests/test_voice.py`

1. 音声会話の model を `gpt-realtime-2.1-mini` に固定する。通常 agent の effort/recipe を Realtime セッションへ渡さない。Realtime mini が受け付ける正式な reasoning 設定だけを adapter に残し、非対応なら `reasoning` payload を送らない。
2. `get_schedule`、`get_status`、held cache で答えられる脊髄反射的な質問は Realtime tool のまま即時回答する。次の予定もこの経路で答え、研究 agent を呼ばない。
3. 研究・授業・仕事の内容質問は `ask_agent` handoff に置き換える。Realtime は tool call 前に「ちょっと確認してみるね」と短く話し、対象 actor を request 内容または軽量 router で選び、その actor の選択 provider・用途レシピに委譲する。
4. handoff は read-only、短い音声用 prompt、25 秒の待機上限を維持する。完了後は 3 文以内の話し言葉へ整形して音声へ戻す。未選択 provider、quota、connector不足、timeout はその理由を短く音声で返し、別 provider へ逃がさない。
5. 旧 `think.Codex` の session persistence と Codex 固定 effort を削除し、agent runner の既存 conversation/session policy に一本化する。音声から外部書き込み・実験実行・Slack 送信は行わない。
6. 作業依頼の `propose_request` → 利用者の読み上げ確認 → `send_request` は現在のまま維持する。

Tests first:

- session payload が mini を指定し、通常 agent effort を含めないことを確認する。
- schedule/status は handoff なしで返り、内容質問は「確認中」メッセージの後に選択 provider の agent を一度だけ呼ぶことを確認する。
- handoff の quota/timeout/capability failure で fallback が起きず、読み上げ可能な失敗文になることを確認する。
- work request は確認なしに送信されない既存テストを維持する。

## 7. quota・通知・評価を実装して運用可能にする

**Files:**

- Modify: `src/kei_agent/store.py`
- Modify: `src/kei_agent/assistant.py`
- Modify: `src/kei_agent/runner.py`
- Modify: `src/kei_agent_voice/tools.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_assistant.py`
- Modify: `tests/test_voice.py`

1. provider/model/use case ごとに開始・成功・失敗・quota stop を状態 DB に記録する。API token や prompt 本文は記録しない。
2. quota error を provider/model/use case/reset estimate（取得できる場合）の構造化結果にする。Slack と音声では「止めた」ことと、選択変更または reset を待つことだけを通知する。
3. Realtime API の利用量/金額上限は subscription quota と別の budget key として扱う。上限に達したら音声会話を止め、通常 agent の provider は変えない。
4. representative fixture を用意して、各 provider/use case を「正確さ・根拠 locator・不明を埋めない・正しい tool 選択・妥当な待ち時間/上限」で評価できる形にする。実 API を通常テストで呼ばず、手動 acceptance run を別コマンドにする。

Tests first:

- quota を受けると同じ/別 provider の自動再試行が起きず、DB と通知に provider/model/use case が残ることを確認する。
- Realtime budget stop が voice のみを止め、Slack agent の profile を変更しないことを確認する。

## 8. ドキュメント・移行案内・全体検証

**Files:**

- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/model-policy.md`
- Modify: `docs/agents.md`
- Modify: `docs/voice.md`
- Modify: `docs/using.md`
- Modify: `docs/design.md`
- Modify: `config.toml`

1. 現在 dirty な `README.md`、`CONTRIBUTING.md`、`docs/using.md` はユーザー変更を上書きしない。実装開始時に差分を再確認し、モデル方針の該当段落だけを最小パッチで更新する。競合して安全に編集できなければ、その時点で利用者へ確認する。
2. 方針文書に allowlist、provider の同等選択、provider 間 effort 非同等、Astra/Fable の手動限定、quota 時 no fallback、connector preflight、用途表、音声の二段経路を記載する。
3. App Home 初期状態（5 actor 全て未選択）と最初に選択すべき provider を案内する。現在の ChatGPT Plus / Claude Pro の購読枠と Realtime API の別課金を混同しない説明にする。
4. `docs/voice.md` から `gpt-realtime-2.1` と Codex 固定 thinker の記述を除き、`gpt-realtime-2.1-mini` と provider-respecting handoff の記述へ更新する。

Verification commands:

```bash
uv run pytest tests/test_model_policy.py tests/test_themes.py tests/test_settings.py tests/test_home.py -q
uv run pytest tests/test_runner.py tests/test_agent_claude.py tests/test_research_agent.py tests/test_assistant.py tests/test_schedule.py -q
uv run pytest tests/test_voice.py -q
uv run pytest -q
git diff --check
```

Manual acceptance checklist:

1. App Home で研究を Codex、授業を Claude、router を Claude、自己改善を Codex に選び、用途ごとに表の対応モデル・effort が command log に一度だけ出ることを確認する。
2. 各 provider の代表的な研究・授業・仕事入力で、根拠位置、未知の明示、正しい connector 選択を確認する。
3. quota/connector不足を注入し、provider が変わらず明示 stop になることを確認する。
4. 音声で予定質問は即時、内容質問は「ちょっと確認してみるね」後の handoff、作業依頼は確認後送信になることを確認する。

## Review focus

- model 名の allowlist が runner、App Home、A2A、voice のいずれかで迂回できないか。
- App Home の provider 選択が global default や model override を復活させていないか。
- provider/connector/quota の失敗で別 provider・強いモデル・manual exception に暗黙 fallback していないか。
- night、scheduler、self-fix、voice が use case を捨てて固定モデルに戻っていないか。
- 既存の user-owned documentation edits と自己改善の承認・worktree・guard の安全柵を壊していないか。

## Self-review

- [x] ユーザーが確定した allowlist と用途別の model/effort をすべて反映した。
- [x] 「agent が provider、skill は recipe 継承、例外 skill のみ専用 recipe」を用途解決器に落とした。
- [x] provider 未選択の router/self_fix を明示停止する決定を反映した。
- [x] Slack/Toggl 計画をこの実装スコープから除外した。
- [x] テストを先に書く順序と、全体検証・手動受入条件を記載した。
