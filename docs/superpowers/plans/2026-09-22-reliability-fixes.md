# Reliability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A2A 認証、外部依頼キュー、音声設定・通知の不具合を直し、Notion と自己改善の説明を実装に一致させる。

**Architecture:** 既存の責務境界を維持し、認証は A2A サーバー境界、依頼の所有権移動は `ask.py`、音声接続は `live.py` と `session.py` に閉じ込める。各変更はテストを先に失敗させ、独立して通した後に全体検証する。

**Tech Stack:** Python 3.13、asyncio、Starlette、aiohttp、SQLite、pytest、pytest-asyncio、ruff

**Spec:** `docs/superpowers/specs/2026-09-22-reliability-fixes-design.md`

## Global Constraints

- A2A の仕事 API は空でない `KEI_AGENT_A2A_TOKEN` を必須とする。
- 公開するのは agent card と health endpoint だけとする。
- 音声通知だけではマイクを開かない。
- 大学 Claude の Notion 更新・作成権限は維持し、削除・移動・複製は許可しない。
- `docs/voice.md` の既存未コミット変更を保持する。
- ユーザーから明示的に依頼されていないため、コミットと push は行わない。

---

### Task 1: A2A トークンを必須にする

**Files:**
- Modify: `src/kei_agent_a2a/server.py:31-75`
- Modify: `src/kei_agent_course/app.py:14-22`
- Modify: `src/kei_agent_research/app.py:14-22`
- Modify: `src/kei_agent_work/app.py:14-22`
- Modify: `src/kei_agent_voice/app.py:20-30`
- Test: `tests/test_a2a.py`

**Interfaces:**
- Produces: `require_token(token: str) -> str`。空または空白だけなら `RuntimeError`、それ以外は元の値を返す。
- Consumes: 各 `build_app(..., token: str)` と `serve()` が `require_token` を通した値。

- [ ] **Step 1: トークンなしを拒否するテストを書く**

```python
def test_a2a_app_refuses_to_start_without_a_password():
    from kei_agent_course.app import build_app
    with pytest.raises(RuntimeError, match="KEI_AGENT_A2A_TOKEN"):
        build_app("http://127.0.0.1:8787", "")

def test_a2a_app_refuses_a_blank_password():
    from kei_agent_course.app import build_app
    with pytest.raises(RuntimeError, match="KEI_AGENT_A2A_TOKEN"):
        build_app("http://127.0.0.1:8787", "   ")
```

- [ ] **Step 2: RED を確認する**

Run: `.venv/bin/python -m pytest tests/test_a2a.py::test_a2a_app_refuses_to_start_without_a_password tests/test_a2a.py::test_a2a_app_refuses_a_blank_password -q`

Expected: 例外が発生せず FAIL。

- [ ] **Step 3: 共通境界でトークンを検証する**

```python
def require_token(token: str) -> str:
    if not token.strip():
        raise RuntimeError(f"{TOKEN_ENV} がありません。A2A の仕事 API は合言葉なしでは起動できません")
    return token

def build_app(..., token: str) -> Starlette:
    token = require_token(token)
    # middleware は常に SharedTokenAuth を付ける
```

`serve()` の警告分岐は削除し、環境変数を `require_token` へ渡す。各ドメインの `build_app` は既定値を持たず、テストを含む全呼び出し側に明示トークンを要求する。

- [ ] **Step 4: GREEN と既存認証挙動を確認する**

Run: `.venv/bin/python -m pytest tests/test_a2a.py tests/test_research_agent.py tests/test_work_agent.py -q`

Expected: 全件 PASS。agent card は無認証で読め、誤った Bearer token は拒否される。

- [ ] **Step 5: 作業状態を確認する**

Run: `git diff --check && git diff -- src/kei_agent_a2a/server.py src/kei_agent_course/app.py src/kei_agent_research/app.py src/kei_agent_work/app.py src/kei_agent_voice/app.py tests/test_a2a.py`

Expected: whitespace error なし。コミットはしない。

### Task 2: 外部依頼に所有権と再試行を持たせる

**Files:**
- Modify: `src/kei_agent/ask.py:14-55`
- Modify: `src/kei_agent/assistant.py:837-873`
- Test: `tests/test_assistant.py:780-830`
- Test: `tests/test_voice.py:230-255`

**Interfaces:**
- Produces: `PendingAsk(path: Path, payload: dict)`、`claim_asks(config) -> list[PendingAsk]`、`complete_ask(item) -> None`、`retry_ask(item) -> None`。
- Consumes: `Assistant.handle_asks()` は claim 済み項目だけを処理し、成功・永久失敗・一時失敗を明示的に完了させる。

- [ ] **Step 1: 一時失敗で依頼が残るテストを書く**

```python
async def test_ask_from_outside_is_retried_when_slack_post_fails(env, config, monkeypatch):
    assistant, slack, _, _ = env
    from kei_agent import ask
    path = ask.write_ask(config, "vlm", "集計して")
    monkeypatch.setattr(slack, "chat_postMessage", AsyncMock(side_effect=RuntimeError("temporary")))
    with pytest.raises(RuntimeError, match="temporary"):
        await assistant.handle_asks()
    assert path.exists()
    assert ask.pending_asks(config)[0][1]["text"] == "集計して"
```

`submit` が例外になった場合も同じく pending に戻るテスト、不正テーマは通知後に消える既存テストを残す。

- [ ] **Step 2: RED を確認する**

Run: `.venv/bin/python -m pytest tests/test_assistant.py::test_ask_from_outside_is_retried_when_slack_post_fails -q`

Expected: 元ファイルが先に削除されているため FAIL。

- [ ] **Step 3: claim/complete/retry を最小実装する**

```python
@dataclass(frozen=True)
class PendingAsk:
    path: Path
    payload: dict

def claim_asks(config: Config) -> list[PendingAsk]:
    # 起動後に残った *.processing を *.json へ戻す
    # 古い順の *.json を os.replace(path, processing_path) で取得する

def complete_ask(item: PendingAsk) -> None:
    item.path.unlink(missing_ok=True)

def retry_ask(item: PendingAsk) -> None:
    os.replace(item.path, item.path.with_suffix(".json"))
```

processing 名は元の一意名を保持する `name.json.processing` とし、`retry_ask` で正確に `name.json` に戻す。JSON破損は claim 時に削除する。

- [ ] **Step 4: `handle_asks` を状態遷移に合わせる**

```python
for item in ask.claim_asks(self.config):
    try:
        # 入力検証。永久失敗なら通知して complete
        # Slack 投稿と submit。成功なら complete
    except Exception:
        ask.retry_ask(item)
        raise
```

`note` は Slack 投稿成功後に完了、`request` は `submit` が例外なく返った後に完了する。

- [ ] **Step 5: GREEN と回収動作を確認する**

Run: `.venv/bin/python -m pytest tests/test_assistant.py -k 'ask_from_outside or note_from_outside or unknown_theme' tests/test_voice.py -k 'pending_asks' -q`

Expected: 全件 PASS。追加で、事前に置いた `.processing` が次回 `claim_asks` で回収されるテストも PASS。

- [ ] **Step 6: 作業状態を確認する**

Run: `git diff --check && git diff -- src/kei_agent/ask.py src/kei_agent/assistant.py tests/test_assistant.py tests/test_voice.py`

Expected: whitespace error なし。コミットはしない。

### Task 3: voice 起動時に「聞く」を復元する

**Files:**
- Modify: `src/kei_agent_voice/app.py:20-52`
- Test: `tests/test_voice.py`

**Interfaces:**
- Produces: `_ears(executor, config: Config | None = None, store: Store | None = None)`。
- Consumes: `settings.listening_enabled(store)` の値を `VoiceSession.run(listening=...)` へ渡す。

- [ ] **Step 1: 保存値を起動初期値へ渡すテストを書く**

```python
async def test_voice_restores_listening_setting_on_start(config, store, monkeypatch):
    settings.set_listening(store, True)
    seen = []
    class FakeSession:
        async def run(self, listening=False):
            seen.append(listening)
            await asyncio.Event().wait()
        def set_listening(self, on): pass
    monkeypatch.setattr(app, "VoiceSession", lambda held, config=None: FakeSession())
    async with app._ears(VoiceExecutor(), config=config, store=store)(None):
        await asyncio.sleep(0)
    assert seen == [True]
```

- [ ] **Step 2: RED を確認する**

Run: `.venv/bin/python -m pytest tests/test_voice.py::test_voice_restores_listening_setting_on_start -q`

Expected: `run()` に `False` が渡って FAIL。

- [ ] **Step 3: 共有 SQLite から初期値を読む**

`_ears` 内で `config or load_config()` と `store or Store(config.db_path)` を使い、`settings.listening_enabled(store)` を一度読んで `session.run(listening=initial_listening)` に渡す。テスト注入した `Store` は閉じず、本番で生成した `Store` だけ lifespan 終了時に `store.conn.close()` で閉じる。

- [ ] **Step 4: GREEN を確認する**

Run: `.venv/bin/python -m pytest tests/test_voice.py -k 'restores_listening or microphone' -q`

Expected: 全件 PASS。

- [ ] **Step 5: 作業状態を確認する**

Run: `git diff --check && git diff -- src/kei_agent_voice/app.py tests/test_voice.py`

Expected: whitespace error なし。コミットはしない。

### Task 4: マイクなしの短命通知セッションを追加する

**Files:**
- Modify: `src/kei_agent_voice/live.py:120-240`
- Modify: `src/kei_agent_voice/session.py:25-95`
- Test: `tests/test_voice.py:280-460`

**Interfaces:**
- Produces: `Live.say_once(text: str, on_said: Callable | None = None) -> None`。マイクを開かず、1件の通知音声が完了したら返る。
- Produces: `VoiceSession.announce(text, expression)` は会話中なら既存接続、非会話中ならFIFO通知workerへ渡す。

- [ ] **Step 1: `Live.say_once` がマイクを開かないテストを書く**

Fake WebSocket に対し、`session.update`、通知の `conversation.item.create`、`response.create` が送られ、`response.output_audio.done` で終了することを確認する。`Microphone` を起動する `_send_microphone` が呼ばれたらテストを失敗させる。

```python
async def test_say_once_sends_one_notice_without_opening_microphone(monkeypatch):
    live = Live(FakeTools(), key="test")
    monkeypatch.setattr(live, "_send_microphone", AsyncMock(side_effect=AssertionError("mic opened")))
    await live.say_once("終わったよ")
    assert sent[-2]["item"]["content"][0]["text"] == "（お知らせ）終わったよ"
    assert sent[-1] == {"type": "response.create"}
```

- [ ] **Step 2: RED を確認する**

Run: `.venv/bin/python -m pytest tests/test_voice.py::test_say_once_sends_one_notice_without_opening_microphone -q`

Expected: `Live.say_once` がなく FAIL。

- [ ] **Step 3: 通知専用接続を最小実装する**

`say_once` は通常と同じ認証・`session.update` を使うが、`_send_microphone` と tool 実行は開始しない。通知イベントを送った後、音声deltaをSpeakerへ渡し、transcriptをjournal callbackへ渡し、`response.output_audio.done` または通知応答の `response.done` を確認して戻る。`finally` で `speaker.stop()`、WebSocket参照の解除、HTTP/WebSocket closeを保証する。

- [ ] **Step 4: 非会話中通知のFIFOテストを書く**

```python
async def test_notice_is_spoken_with_short_session_when_not_listening(config):
    brain = _FakeBrain()
    session = _session_for(config, brain)
    await session.run_in_background_for_test()
    session.announce("一件目")
    session.announce("二件目")
    await session.wait_for_notices_for_test()
    assert brain.said_once == ["一件目", "二件目"]
```

公開APIにテスト専用メソッドは作らず、実際には `run()` をtask化し、FakeBrainのEventで完了を待つ。会話中は既存の `brain.announce` だけが呼ばれ、`say_once` は呼ばれないテストも置く。

- [ ] **Step 5: RED を確認する**

Run: `.venv/bin/python -m pytest tests/test_voice.py -k 'short_session or notice_is_spoken or existing_connection' -q`

Expected: 現状は非会話中通知を捨てるため FAIL。

- [ ] **Step 6: `VoiceSession` に通知workerを実装する**

`VoiceSession` に `asyncio.Queue[str]` と `_notice_worker` taskを持たせる。`run()` の開始時にworkerを起動し、終了時にcancelしてawaitする。`announce()` は顔を即時更新し、会話中なら `brain.announce`、非会話中ならqueueへ投入する。workerは `await brain.say_once(text, on_said=self._write)` を順番に実行し、成功時に同じ通知を二重記録しない。`Unavailable` と通常例外はログに残して次の通知へ進む。

- [ ] **Step 7: GREEN と終了処理を確認する**

Run: `.venv/bin/python -m pytest tests/test_voice.py -q`

Expected: 全件 PASS。終了後に通知workerがdoneで、会話中と非会話中の双方がジャーナルへ1回だけ記録される。

- [ ] **Step 8: 作業状態を確認する**

Run: `git diff --check && git diff -- src/kei_agent_voice/live.py src/kei_agent_voice/session.py tests/test_voice.py`

Expected: whitespace error なし。コミットはしない。

### Task 5: 運用ドキュメントを正式仕様へ合わせる

**Files:**
- Modify: `deploy/README.md:47-90,212-230`
- Modify: `docs/agents.md:73-92,115`
- Modify: `README.md:53-55`
- Modify: `docs/using.md:95-105`
- Modify: `docs/voice.md`（ユーザーの既存変更を保持し、通知仕様の該当箇所だけ編集）

**Interfaces:**
- Consumes: Task 1 の必須トークン、Task 3・4 の音声挙動、承認済みNotion権限。
- Produces: セットアップ時に必要な秘密情報と実際のユーザー向け挙動が一致した文書。

- [ ] **Step 1: A2Aトークン設定を追記する**

`deploy/README.md` の共通秘密情報に次を追加する。

```zsh
# 本体と全A2Aエージェントで共通。空だとA2Aエージェントは起動しない
export KEI_AGENT_A2A_TOKEN="$(openssl rand -hex 32)"
```

コマンド置換結果を一度ファイルへ保存し、各プロセスで同じ値を読む必要があることを文章で明記する。

- [ ] **Step 2: Notion権限の説明を統一する**

`docs/agents.md` と `deploy/README.md` を次の内容へ合わせる。

- Box は読み取り専用。
- Notion は授業・課題の読み取り、課題の状態更新、課題ページ作成を許可。
- 削除・移動・複製・DB作成は不許可。
- コネクタはツールだけで対象ページを限定できないため、専用Claudeプロファイル、Notion側の共有範囲、`prompts/course.md` で制約。

- [ ] **Step 3: 音声と自己改善の説明を統一する**

`docs/using.md` と `docs/voice.md` に、「知らせる」だけなら通知時だけ接続し、マイクは開かないこと、「聞く」は保存値から再起動後も復元されることを書く。`README.md` は、自己改善が案・実装・取り込みの承認後にpushされる説明へ直す。

- [ ] **Step 4: 文書差分を確認する**

Run: `git diff --check && git diff -- README.md deploy/README.md docs/agents.md docs/using.md docs/voice.md`

Expected: `docs/voice.md` 冒頭の既存変更が保持され、実装と矛盾する「読むだけ」「聞いていなければ喋らない」が残っていない。コミットはしない。

### Task 6: 全体検証

**Files:**
- Verify only: repository-wide

**Interfaces:**
- Consumes: Task 1–5 の全変更。
- Produces: テスト・静的検査・差分監査の結果。

- [ ] **Step 1: 全テストを実行する**

Run: `.venv/bin/python -m pytest -q`

Expected: 377件の既存テストと追加テストがすべて PASS。依存先 `a2a-sdk` の既知のDeprecationWarning以外に新しいwarningなし。

- [ ] **Step 2: lintを実行する**

Run: `uvx ruff check .`

Expected: `All checks passed!`

- [ ] **Step 3: 差分を監査する**

Run: `git diff --check && git status --short && git diff --stat`

Expected: whitespace errorなし。対象外のA2A executor共通化と`Assistant`分割が混入していない。秘密情報の実値が含まれていない。

- [ ] **Step 4: 完了報告用の結果を記録する**

テスト件数、lint結果、変更ファイル、残した既知warning、未実施の第2段階をまとめる。コミットとpushは行わない。
