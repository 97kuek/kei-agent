# 授業の Notion の作り直し Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 授業ホームを「授業時間表のページ＋授業・課題・成績・単位要件の4つの DB（列名と選択肢は英語、列は決まった順）」の形にし、Kei Agent のコード・Dot のプロンプト・Notion の中身を切り替える。

**Architecture:** 授業ホームの作りは `modules/course/notion_setup.py`（列の定義・値の定数・`CourseSetup`）が持ち、読み書きは `notion_sync.py`（課題と授業）・`submissions.py`（提出の確認）・`academic_sync.py`（成績と単位要件）が値の定数を `notion_setup.py` から読んで使う。つながりの列は `CourseSetup._resolve` が相手の data_source_id に置き換えて作り、表の列の順番は `CourseSetup.order_views` がビューでそろえる（Notion は戻り側の列を右端に足すため）。GPA は保存せず、成績に当たる授業が無いときは成績の取り込みが「終わった授業」を1度だけ作る。

**Tech Stack:** Python 3（uv）、pytest、ruff、Notion API（ゲートウェイ経由、client は course）、Dot（ChatGPT）のプロンプト

**Spec:** `docs/superpowers/specs/2026-10-04-course-notion-design.md`

## Global Constraints

- 授業ホームの並び: 授業時間表（ページ）→ 授業 → 課題 → 成績 → 単位要件
- 列の順番: 名前のすぐ右に、いちばんよく見る列（状態・成績・残り）→ 時間（年度・学期・締切）と数字（単位など）→ 右端に、つながりと照合キー。DB はこの順に列を作り、表の表示もこの順にそろえる
- 授業: Name（title）・Status（select: Taking / Done）・Year（number）・Term（select）・Day（select）・Period（number）・Credits（number）・Moodle（url）。照合キーは Name＋Year＋Term
- 課題: Name（title）・Status（status: Not started / In progress / Submitted / Overdue）・Due（date）・Course（relation → 授業）・Link（url）・Moodle ID（rich_text。空なら手入力の課題）
- 成績: Name（title）・Grade（rich_text）・GP（number）・Credits（number）・Category（rich_text、科目区分）・Group（rich_text、科目群）・Course（relation → 授業）・Requirement（relation → 単位要件）・Record ID（rich_text）
- 単位要件: Name（title）・Remaining（number）・Required（number）・Counted（number）・Group（rich_text、大区分）・Kind（select: Category / Subtotal / Total / Other）・Grades（relation → 成績。成績の Requirement の戻り側）・Record ID（rich_text）
- 取得済みかは Grade が不可（F）でないことで判断する。GPA は保存しない（Dot が GP×単位の合計÷単位の合計で計算する）。既得単位は取り込まない
- なくす DB: GPA推移・学習ログ（勉強時間は共通ホームの「時間記録」（領域＝大学））
- 消す列: 授業の科目コード・履修年次・科目区分・科目群、課題の出どころ・見積時間・実績時間・最終同期、成績の取得年度・学期・取得済・取り込み元・GPA推移、単位要件の既得単位・対象授業・取り込み元
- 値の写し方: 授業の状態 履修中→Taking、終了→Done。課題の状態 未着手→Not started、進行中→In progress、提出済み→Submitted、期限切れ→Overdue。単位要件の集計種別 区分→Category、小計→Subtotal、総合計→Total、その他→Other
- 成績履歴の DB の名前は「成績」にする
- モジュールがコアに触れるのは窓口（`kei_agent_a2a.api`・`kei_agent.api`）だけ。コア（`src/kei_agent/`）は `modules/` を読み込まない（`tests/test_layers.py`）
- コメント・文書・Slack の文は日本語。文書には今の状態だけを書く
- 確かめる: `uv run python -m pytest` と `uvx ruff check .`（sandbox の外で）
- コミットの1行目は `feat:`・`fix:`・`docs:` などを頭に付けた英語の短い文
- 本物の Notion・Dot の設定に触れるのは Task 7 だけで、そのときも利用者の確認を取ってから（Task 6 はリポジトリのプロンプトの文だけを変える）

## Review Focus

- 移し替えが済んでいないホーム（題の列が「科目名」のまま、または「📊 成績履歴」だけがある）で `setup` を動かす → 英語の列や空の「成績」DB を足さずに、どの DB かを書いた NotionError で何も書く前に止まる（Task 1）
- 成績の Requirement は、単位要件の Grades を作ったときに Notion が成績の右端（Record ID の右）に足す → 表のビューでは Course と Record ID のあいだに並ぶ。2回目の setup はビューを書き直さない（Task 1）
- 手で Overdue にした課題・Submitted の課題の Due がまだ先 → 締切一覧（`list-due`）に出さない（Task 2）
- 学校の部品が単位要件の Kind に選択肢に無い値（例「区分」）を返す → 新しい選択肢を作らず Other として書く（Task 3）
- 「授業」に無い科目の成績を2回取り込む → 終わった授業（Status＝Done）を1度だけ作って結び、2回目は授業も成績も単位要件も2行にしない（Task 3）

---

## 順番の前提

- このプランは、研究の Notion のプラン（ブランチ `feat/research-notion`、`docs/superpowers/plans/2026-10-04-research-notion.md`）が main に入ってから実行する。どちらも `src/kei_agent/testing/fakes.py` と Notion の共通コード（`src/kei_agent/storage/notion.py` の `Setup`）を変えるため
- 始める前に `git fetch origin && git rebase origin/main` でこのブランチを main に載せ直し、main から `feat/course-notion` を切って作業する。研究のプランで行番号がずれた箇所は、下に書いたメソッド名・関数名で探す
- 研究のプランは `Setup.database()` から `relations` の処理を消す。このプランのつながりの列は `CourseSetup._resolve` が作るので、`relations` には頼らない
- Task 6（Dot のプロンプト）は、PR [97kuek/kei-agent#24](https://github.com/97kuek/kei-agent/pull/24) を main に入れてから、作業ブランチに main を取り込んで進める
- Task 7（本物の Notion の移し替え）は、Task 1〜6 の PR をマージする直前から始める。古いコードが新しい列に書こうとしても、新しいコードが古い列に書こうとしても、Notion が要求ごと断るだけで行は壊れない（失敗した見回りは10分後にやり直す）。それでも空く時間は短くする

## File Structure

| ファイル | 役目 | 変えること |
|---|---|---|
| `modules/course/notion_setup.py` | 授業ホームの定義と setup | 4つの DB を英語の列と順番にする。値の定数を置く。授業時間表のページを作る。移し替え前のホームで止める。表のビューの列をそろえる。GPA推移と `TITLE_ALIASES` を消す |
| `modules/course/academic_record.py` | 成績の記録の形 | Kind の定数を置く。GPA と既得単位を消す |
| `modules/course/notion_sync.py` | 課題の取り込みと授業の読み取り | 新しい列名と値にする。出どころを書かない。GPA推移を要らない DB にする |
| `modules/course/submissions.py` | 提出の確認 | Status を Submitted にする。Link を補う |
| `modules/course/agent.py` | 担当プロセス | 締切一覧から Submitted と Overdue を除く。説明の文 |
| `src/kei_agent/operations/hands_server.py` | MCP の道具の説明 | `sync_submissions` の説明を Submitted にする |
| `modules/course/academic_sync.py` | 成績と単位要件の取り込み | 新しい列名にする。授業に Category・Group を書かない。GPA の処理を消す。足りない授業は取り込みで作る。単位要件とのつながりだけを結ぶ |
| `modules/course/school.py`・`modules/course/schools/waseda.py` | 学校の部品 | GPA と科目群の対応を消す。Kind を英語で返す |
| `src/kei_agent/testing/fakes.py` | 偽物 | `FakeNotionAPI` が dual_property のつながりの戻り側の列を作るようにする |
| `src/kei_agent/storage/notion_hub_setup.py` | 共通ホームの「今週のタスク」 | 授業課題の表を Due・Status・Submitted で絞る |
| `docs/agents/course-agent.md` | 大学の文書 | 授業ホームの形・コマンド・提出の確認の書き方 |
| `docs/prompts/` | Dot のプロンプト | 新しい列名と値にする |
| `tests/test_course_setup.py`（新規）ほか `tests/test_course_*.py`・`tests/test_academic_*.py`・`tests/test_catalog.py`・`tests/test_moodle_submissions.py`・`tests/test_a2a.py`・`tests/test_notion_hub.py` | テスト | 新しい形にする |

`modules/course/moodle.py`・`ics.py` は Notion の列に触れないので変えない。`modules/course/toggl_report.py` と `agent.py` の `time-report` は Toggl の記録を数えていて、課題の実績時間を読んでいない（Task 5 の Step 1 で確かめる）。

---

### Task 1: 授業ホームの定義と setup

**Files:**
- Modify: `modules/course/notion_setup.py:1-265`（`main()` の 268 行目以降はそのまま）
- Modify: `modules/course/academic_record.py:13`（定数を足す）
- Modify: `src/kei_agent/testing/fakes.py:313-322`（`add_database`）・`src/kei_agent/testing/fakes.py:545-551`（`_data_sources` の PATCH）
- Create: `tests/test_course_setup.py`
- Modify: `tests/test_course_school.py:88-96`
- Modify: `tests/test_course_ics.py:118-166`（消す）
- Modify: `tests/test_academic_sync.py:1,5,9,14-139`（消す）

**Interfaces:**
- Produces:
  - `academic_record.KIND_CATEGORY = "Category"`・`KIND_SUBTOTAL = "Subtotal"`・`KIND_TOTAL = "Total"`・`KIND_OTHER = "Other"`・`KINDS = (KIND_CATEGORY, KIND_SUBTOTAL, KIND_TOTAL, KIND_OTHER)`
  - `notion_setup.TAKING = "Taking"`・`DONE = "Done"`・`NOT_STARTED = "Not started"`・`IN_PROGRESS = "In progress"`・`SUBMITTED = "Submitted"`・`OVERDUE = "Overdue"`
  - `notion_setup.TIMETABLE_TITLE = "授業時間表"`、`LEGACY_TITLES = {"grades": "📊 成績履歴", "requirements": "🎓 単位要件"}`
  - `COURSES`・`ASSIGNMENTS`・`GRADES`・`REQUIREMENTS: dict`（`icon`・`description`・`properties`。つながりの列は `_relation(target, synced)`、戻り側は `_back(target, synced)` の印）
  - `SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS), "grades": ("成績", GRADES), "requirements": ("単位要件", REQUIREMENTS)}`
  - `course_spec(school: School) -> dict`、`specs(school: School) -> dict[str, tuple[str, dict]]`、`timetable_blocks(school: School) -> list[dict]`
  - `CourseSetup.run(courses: list[Course] | None = None, year: int | None = None) -> None`、`CourseSetup.check_home(children: list[dict]) -> None`、`CourseSetup.timetable_page(children: list[dict]) -> str`、`CourseSetup.order_views(info: dict, spec: dict) -> None`
  - 消すもの: `GPA`・`TITLE_ALIASES`・`CourseSetup.database` の上書き
  - `FakeNotionAPI` は dual_property のつながりを作ると、相手のデータソースに戻り側の列を足す

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_course_setup.py
"""授業ホームの作り（授業時間表のページと4つの DB の列・順番・選択肢）。本物の Notion は使わない。"""

import pytest

from kei_agent.storage.notion import Notion, NotionError
from kei_agent.testing.fakes import FakeNotionAPI
from kei_agent_modules.course import notion_setup, school
from kei_agent_modules.course.notion_setup import CourseSetup

WASEDA = school.load({"school": "waseda"})
COURSE_COLUMNS = ["Name", "Status", "Year", "Term", "Day", "Period", "Credits", "Moodle"]
ASSIGNMENT_COLUMNS = ["Name", "Status", "Due", "Course", "Link", "Moodle ID"]
GRADE_COLUMNS = ["Name", "Grade", "GP", "Credits", "Category", "Group", "Course", "Requirement", "Record ID"]
REQUIREMENT_COLUMNS = ["Name", "Remaining", "Required", "Counted", "Group", "Kind", "Grades", "Record ID"]


def client(api: FakeNotionAPI):
    """FakeNotionAPI に、Notion と同じ paginate と children を付ける。"""
    class Client:
        request = staticmethod(api.request)

        def paginate(self, method, path, body=None):
            return Notion.paginate(self, method, path, body)

        def children(self, block_id):
            return Notion.children(self, block_id)

    return Client()


def options(spec: dict, name: str) -> list[str]:
    kind = next(iter(spec["properties"][name]))
    return [option["name"] for option in spec["properties"][name][kind]["options"]]


def columns(api: FakeNotionAPI, setup: CourseSetup, key: str) -> list[str]:
    ds = setup.state["databases"][key]["data_source_id"]
    return list(api.items[api.key(ds)]["properties"])


def home_children(api: FakeNotionAPI, home: str) -> list[tuple[str, str]]:
    return [(b["type"], (b.get("child_page") or b.get("child_database") or {}).get("title"))
            for b in client(api).children(home)]


def new_home(tmp_path):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    return api, home, CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA)


def test_columns_and_options_follow_the_spec():
    assert list(notion_setup.COURSES["properties"]) == COURSE_COLUMNS
    assert options(notion_setup.COURSES, "Status") == ["Taking", "Done"]
    assert list(notion_setup.ASSIGNMENTS["properties"]) == ASSIGNMENT_COLUMNS
    assert options(notion_setup.ASSIGNMENTS, "Status") == ["Not started", "In progress", "Submitted", "Overdue"]
    assert list(notion_setup.GRADES["properties"]) == GRADE_COLUMNS
    assert list(notion_setup.REQUIREMENTS["properties"]) == REQUIREMENT_COLUMNS
    assert options(notion_setup.REQUIREMENTS, "Kind") == ["Category", "Subtotal", "Total", "Other"]
    assert [title for title, _spec in notion_setup.SPECS.values()] == ["授業", "課題", "成績", "単位要件"]


def test_setup_puts_the_timetable_first_and_creates_columns_in_order(tmp_path):
    api, home, setup = new_home(tmp_path)
    setup.run()

    assert home_children(api, home) == [("child_page", "授業時間表"), ("child_database", "授業"),
                                        ("child_database", "課題"), ("child_database", "成績"),
                                        ("child_database", "単位要件")]
    assert columns(api, setup, "courses") == COURSE_COLUMNS
    assert columns(api, setup, "assignments") == ASSIGNMENT_COLUMNS
    # 戻り側の Requirement は、単位要件の Grades を作ったときに Notion が成績の右端に足す
    assert columns(api, setup, "grades") == [*GRADE_COLUMNS[:7], "Record ID", "Requirement"]
    assert columns(api, setup, "requirements") == REQUIREMENT_COLUMNS
    assert set(setup.state["databases"]) == {"courses", "assignments", "grades", "requirements"}
    timetable = next(b["id"] for b in client(api).children(home) if b["type"] == "child_page")
    lines = [b["bulleted_list_item"]["rich_text"][0]["plain_text"] for b in client(api).children(timetable)]
    assert lines[:2] == ["1限 08:50-10:30", "2限 10:40-12:20"]


def test_course_links_are_one_way_and_grades_and_requirements_are_two_way(tmp_path):
    api, _home, setup = new_home(tmp_path)
    setup.run()
    props = {key: api.items[api.key(setup.state["databases"][key]["data_source_id"])]["properties"]
             for key in ("courses", "assignments", "grades", "requirements")}

    # 授業に戻り側の列を作らない（授業の列は8つだけ）
    assert not any(p["type"] == "relation" for p in props["courses"].values())
    course = props["assignments"]["Course"]["relation"]
    assert course["type"] == "single_property"
    assert course["data_source_id"] == setup.state["databases"]["courses"]["data_source_id"]
    assert props["grades"]["Course"]["relation"]["type"] == "single_property"
    assert props["requirements"]["Grades"]["relation"]["dual_property"]["synced_property_name"] == "Requirement"
    assert props["grades"]["Requirement"]["relation"]["dual_property"]["synced_property_name"] == "Grades"


def test_table_views_show_the_columns_in_the_spec_order_including_the_back_relation(tmp_path):
    api, home, setup = new_home(tmp_path)
    setup.run()
    db = setup.state["databases"]["grades"]
    view = api.add_view(db["database_id"], "Default view")

    setup.run()

    shown = api.request("GET", f"/views/{view}")["configuration"]["properties"]
    by_id = {p["id"]: name for name, p in api.items[api.key(db["data_source_id"])]["properties"].items()}
    assert [by_id[p["property_id"]] for p in shown] == GRADE_COLUMNS
    assert all(p["visible"] for p in shown)
    # 3回目は同じ形なので、ビューも DB もページも書き直さない
    before, items = len(setup.log), len(api.items)
    setup.run()
    assert setup.log[before:] == [] and len(api.items) == items
    assert [title for _kind, title in home_children(api, home)].count("授業時間表") == 1


@pytest.mark.parametrize(("legacy", "match"), [("title", "科目名"), ("grades", "📊 成績履歴")])
def test_setup_on_a_home_that_is_not_migrated_stops_before_any_write(tmp_path, legacy, match):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    if legacy == "title":
        api.add_database(home, "授業", {"科目名": {"title": {}}, "状態": {"select": {"options": []}}})
    else:
        api.add_database(home, "📊 成績履歴", {"授業名": {"title": {}}})
    before = len(api.items)

    with pytest.raises(NotionError, match=match):
        CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA).run()

    assert len(api.items) == before
    assert not (tmp_path / "notion-course.json").exists()


@pytest.mark.parametrize("duplicate", ["授業", "成績"])
def test_two_databases_with_the_same_name_stop_before_any_write(tmp_path, duplicate):
    api = FakeNotionAPI()
    home = api.add_page(title="授業ホーム")
    for _ in range(2):
        api.add_database(home, duplicate, {"Name": {"title": {}}})
    before = len(api.items)

    with pytest.raises(NotionError, match="重複"):
        CourseSetup(client(api), home, tmp_path / "notion-course.json", WASEDA).run()

    assert len(api.items) == before
```

`tests/test_course_school.py` の `test_course_database_options_come_from_the_school`（88-96 行目）を次にする。

```python
def test_course_database_options_come_from_the_school():
    """「授業」の Term の選択肢は学校から。早稲田は今までと同じ名前。"""
    spec = notion_setup.course_spec(school.load({"school": "waseda"}))["properties"]
    assert [option["name"] for option in spec["Term"]["select"]["options"]] == [
        "春学期", "秋学期", "通年", "春ク", "夏ク", "秋ク", "冬ク", "その他"]
    plain = notion_setup.course_spec(school.School())["properties"]
    assert [option["name"] for option in plain["Term"]["select"]["options"]] == ["その他"]
    assert notion_setup.COURSES["properties"]["Term"]["select"]["options"] == []    # 元の形は書き換えない
```

古い setup のテストを消す。`tests/test_course_ics.py` の「# 授業用 Notion のセットアップ」の見出しと `test_course_setup_creates_six_canonical_databases_and_relations`（118-166 行目）。`tests/test_academic_sync.py` の `import copy`（1 行目）・`from kei_agent.storage.notion import NotionError`（5 行目）・`from kei_agent_modules.course.notion_setup import CourseSetup`（9 行目）と、`_property`・`ExistingCourseHomeNotion`・`test_setup_fits_the_existing_home_without_a_second_title_or_lost_rows`・`test_setup_checks_all_canonical_duplicates_before_any_write`（14-139 行目。重複のテストは上の `test_two_databases_with_the_same_name_stop_before_any_write` に移した）。

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_course_setup.py tests/test_course_school.py -v`
Expected: FAIL（`test_columns_and_options_follow_the_spec` が `['科目名', '科目コード', ...] == ['Name', 'Status', ...]` で落ちる。setup のテストは授業時間表が無い・「📈 GPA推移」ができる・NotionError が出ないことで落ちる。`test_course_database_options_come_from_the_school` は `KeyError: 'Term'`）

- [ ] **Step 3: Kind の定数を足す**

`modules/course/academic_record.py` の import の後ろ（13 行目の空行の次）に足す。

```python
# 単位要件の Kind（授業ホームの「単位要件」の選択肢と同じ）
KIND_CATEGORY, KIND_SUBTOTAL, KIND_TOTAL, KIND_OTHER = "Category", "Subtotal", "Total", "Other"
KINDS = (KIND_CATEGORY, KIND_SUBTOTAL, KIND_TOTAL, KIND_OTHER)
```

- [ ] **Step 4: 偽物の Notion に戻り側の列を作らせる**

`src/kei_agent/testing/fakes.py` の `FakeNotionAPI` に次のメソッドを足し、`add_database` の2つ目の `self._put(...)`（データソースを置くほう）の直後で `self._link_back(ds_id, schema)` を、`_data_sources` の PATCH の `else:` の `ds["properties"].update(self._schema({name: spec}))` の直後で `self._link_back(ds["id"], {name: spec})` を呼ぶ。

```python
    def _link_back(self, ds_id: str, properties: dict) -> None:
        """本物と同じく、dual_property のつながりは相手のデータソースの右端に戻り側の列を作る。"""
        for name, spec in properties.items():
            relation = spec.get("relation") or {}
            if relation.get("type") != "dual_property":
                continue
            target = self.items.get(self.key(relation.get("data_source_id") or ""))
            back = relation["dual_property"]["synced_property_name"]
            if target is None or target["object"] != "data_source" or back in target["properties"]:
                continue
            target["properties"].update(self._schema({back: {"relation": {
                "data_source_id": ds_id, "type": "dual_property",
                "dual_property": {"synced_property_name": name}}}}))
```

- [ ] **Step 5: 定義と setup を書き換える**

`modules/course/notion_setup.py` の 1〜265 行目（docstring から `add_course` まで）を次にする。`add_course` の中身は Task 2 で新しい列にするので、ここでは今のまま残す（下のコードの `add_course` は今と同じ）。

```python
"""授業ホーム（授業時間表のページと、授業・課題・成績・単位要件の4つの DB）を作り、足りない列だけを足す。

Notion はゲートウェイ経由（client は course）で、授業ホームの中だけに届く。
研究のデータベースには触れない（docs/architecture.md）。

使い方（Notion ゲートウェイが動いていること）:
    source ~/.config/kei-agent/secrets/kei-agent.zsh   # 置き場所は config.toml の [paths] secrets
    kei-agent-module course setup [<授業ホームのページID>] [--seed <履修科目のファイル>]

列の名前と選択肢は英語で、列は決まった順に作る（docs/agents/course-agent.md の「授業ホーム（Notion）」）。
「授業」の Term の選択肢は、学校（config.toml の [course] と学校の部品。school.py）から作る。
前の形（題の列が日本語、「📊 成績履歴」など）のホームには、何も書かずに止まる。
履修科目のファイルの書き方は、同じフォルダの courses.example.toml。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from kei_agent_a2a.api import WEEKDAYS, NotionError, Setup, gateway_notion, load_config

from . import notion_props
from .academic_record import KIND_CATEGORY, KIND_OTHER, KIND_SUBTOTAL, KIND_TOTAL
from .course_identity import normalize_course_name
from .school import OTHER, School, SchoolError, from_config

# 授業の Status
TAKING, DONE = "Taking", "Done"
# 課題の Status
NOT_STARTED, IN_PROGRESS, SUBMITTED, OVERDUE = "Not started", "In progress", "Submitted", "Overdue"
TIMETABLE_TITLE = "授業時間表"
# 前の形の DB の名前。新しい名前の DB が無いのにこれがあれば、移し替えが済んでいない
LEGACY_TITLES = {"grades": "📊 成績履歴", "requirements": "🎓 単位要件"}


def _relation(target: str, synced: str | None = None) -> dict:
    """つながりの列。作るときに相手の data_source_id に置き換える（CourseSetup._resolve）。

    synced を渡すと相手にも戻り側の列ができる（dual_property）。渡さなければ片側だけ（single_property）。
    """
    return {"relation": {"target": target, "synced": synced}}


def _back(target: str, synced: str) -> dict:
    """相手の DB の dual_property が作る戻り側の列。setup は作らず、表の列の順番にだけ使う。"""
    return {"relation": {"back_of": target, "synced": synced}}


def _select(*names_colors: tuple[str, str]) -> dict:
    return {"select": {"options": [{"name": name, "color": color} for name, color in names_colors]}}


COURSES = {
    "icon": "📚",
    "description": "履修している科目。Name は Moodle のカレンダーの科目名とそろえる。成績とは Name＋Year＋Term で突き合わせる。",
    "properties": {
        "Name": {"title": {}},
        "Status": _select((TAKING, "green"), (DONE, "gray")),
        "Year": {"number": {"format": "number"}},
        # 選択肢は学校から作る（course_spec）
        "Term": {"select": {"options": []}},
        "Day": _select(("月", "red"), ("火", "orange"), ("水", "yellow"), ("木", "green"),
                       ("金", "blue"), ("土", "purple"), ("日", "pink"), ("他", "gray")),
        "Period": {"number": {"format": "number"}},
        "Credits": {"number": {"format": "number"}},
        "Moodle": {"url": {}},
    },
}

ASSIGNMENTS = {
    "icon": "📝",
    "description": "課題と締切。Moodle ID のある行は Moodle の取り込みで直し、Moodle ID の無い行（手入力）には触らない。",
    "properties": {
        "Name": {"title": {}},
        "Status": {"status": {"options": [
            {"name": NOT_STARTED, "color": "gray", "group": "To-do"},
            {"name": IN_PROGRESS, "color": "blue", "group": "In progress"},
            {"name": SUBMITTED, "color": "green", "group": "Complete"},
            {"name": OVERDUE, "color": "red", "group": "Complete"},
        ]}},
        "Due": {"date": {}},
        "Course": _relation("courses"),
        "Link": {"url": {}},
        # Moodle から取り込むときの照合キー（ics の UID）。空なら手入力の課題
        "Moodle ID": {"rich_text": {}},
    },
}

GRADES = {
    "icon": "📊",
    "description": "成績ページから取り込んだ科目ごとの成績。Grade が F でなければ取得済み。GPA は保存せず、GP と Credits から計算する。",
    "properties": {
        "Name": {"title": {}},
        "Grade": {"rich_text": {}},
        "GP": {"number": {"format": "number"}},
        "Credits": {"number": {"format": "number"}},
        "Category": {"rich_text": {}},
        "Group": {"rich_text": {}},
        "Course": _relation("courses"),
        "Requirement": _back("requirements", "Grades"),
        "Record ID": {"rich_text": {}},
    },
}

REQUIREMENTS = {
    "icon": "🎓",
    "description": "卒業要件の集計。Remaining が残り単位（Kind が Total の行が全体）。成績とは根拠がある場合だけ結ぶ。",
    "properties": {
        "Name": {"title": {}},
        "Remaining": {"number": {"format": "number"}},
        "Required": {"number": {"format": "number"}},
        "Counted": {"number": {"format": "number"}},
        "Group": {"rich_text": {}},
        "Kind": _select((KIND_CATEGORY, "blue"), (KIND_SUBTOTAL, "orange"), (KIND_TOTAL, "red"), (KIND_OTHER, "gray")),
        "Grades": _relation("grades", "Requirement"),
        "Record ID": {"rich_text": {}},
    },
}

# 作る順番（つながりの相手を先に作る）。ホームにもこの順に並ぶ
SPECS = {"courses": ("授業", COURSES), "assignments": ("課題", ASSIGNMENTS),
         "grades": ("成績", GRADES), "requirements": ("単位要件", REQUIREMENTS)}

# 学校から作る選択肢の色（順に使う。「その他」は灰色）
_COLORS = ("green", "orange", "blue", "pink", "purple", "yellow", "red", "brown")
# 履修科目のファイルの「曜日」に書けるもの（「他」は曜日の決まっていない科目）
_WEEKDAYS = (*WEEKDAYS, "他")


def _options(names: list[str]) -> list[dict]:
    names = list(dict.fromkeys(name for name in names if name != OTHER))
    return [{"name": name, "color": _COLORS[n % len(_COLORS)]} for n, name in enumerate(names)] + [
        {"name": OTHER, "color": "gray"}]


def course_spec(school: School) -> dict:
    """「授業」の形。Term の選択肢は学期の設定から作る。"""
    spec = copy.deepcopy(COURSES)
    spec["properties"]["Term"]["select"]["options"] = _options(list(school.terms))
    return spec


def specs(school: School) -> dict[str, tuple[str, dict]]:
    """その学校の、正本の DB の名前と形。"""
    return {**SPECS, "courses": (SPECS["courses"][0], course_spec(school))}


def _block(kind: str, text: str) -> dict:
    return {"type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def timetable_blocks(school: School) -> list[dict]:
    """授業時間表のページの中身。学校の時限の時刻を1行ずつ。分からなければ、書いてほしいと1行だけ。"""
    if not school.periods:
        return [_block("paragraph", "時限の時刻がまだ分かりません。学校の時間割を見て「1限 08:50-10:30」の形で書いてください")]
    return [_block("bulleted_list_item", f"{period}限 {start:%H:%M}-{end:%H:%M}")
            for period, (start, end) in school.periods.items()]


@dataclass(frozen=True)
class Course:
    """履修科目のファイルの1行。学期を省くと、ファイルの term（それも無ければ今日の学期）。"""
    name: str
    weekday: str
    period: int | None = None
    term: str = ""


def read_seed(path: Path) -> tuple[int | None, list[Course]]:
    # （今の 167-189 行目のまま）


class CourseSetup(Setup):
    """授業ホームの先頭に授業時間表のページを置き、4つの DB を作る（すでにあれば、足りない列だけ足す）。"""

    def __init__(self, notion, home_page_id: str, state_path: Path, school: School | None = None):
        super().__init__(notion, home_page_id, state_path)
        self.school = school or School()

    def run(self, courses: list[Course] | None = None, year: int | None = None) -> None:
        children = self.notion.children(self.home)
        self.check_home(children)
        self.timetable_page(children)
        wanted = specs(self.school)
        for key, (title, spec) in wanted.items():
            self.database(key, self.home, title, self._resolve(spec))
        for key, (_title, spec) in wanted.items():
            self.order_views(self.state["databases"][key], spec)
        for course in courses or []:
            self.add_course(course.name, course.weekday, course.period, term=course.term, year=year)

    def check_home(self, children: list[dict]) -> None:
        """書く前に、重複した DB・移し替えの済んでいない DB が無いかを見る。あれば何も書かずに止まる。"""
        wanted = specs(self.school)
        titles = Counter(b["child_database"]["title"] for b in children if b["type"] == "child_database")
        canonical = {title for title, _spec in wanted.values()}
        duplicates = sorted(title for title, count in titles.items() if title in canonical and count > 1)
        if duplicates:
            raise NotionError(f"正本データベースが重複しています: {'、'.join(duplicates)}。正本を確認してから整理してください")
        legacy = sorted(old for key, old in LEGACY_TITLES.items() if old in titles and wanted[key][0] not in titles)
        if legacy:
            raise NotionError(f"前の名前の DB があります: {'、'.join(legacy)}。列と名前を新しい形に移し替えてから setup を"
                              "動かしてください（docs/agents/course-agent.md の「授業ホーム（Notion）」）")
        for block in children:
            title = (block.get("child_database") or {}).get("title")
            if block["type"] != "child_database" or title not in canonical:
                continue
            ds_id = self.notion.request("GET", f"/databases/{block['id']}")["data_sources"][0]["id"]
            props = self.notion.request("GET", f"/data_sources/{ds_id}")["properties"]
            name = next((n for n, p in props.items() if p.get("type") == "title"), "")
            if name != "Name":
                raise NotionError(f"「{title}」の題の列が「{name}」のままです。列の名前を英語に移し替えてから setup を"
                                  "動かしてください（docs/agents/course-agent.md の「授業ホーム（Notion）」）")

    def timetable_page(self, children: list[dict]) -> str:
        """授業時間表のページ。無ければ学校の時限の時刻を書いて作る。あるページの中身には触らない。"""
        for block in children:
            if block["type"] == "child_page" and block["child_page"]["title"] == TIMETABLE_TITLE:
                return block["id"]
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "page_id", "page_id": self.home},
            "icon": {"type": "emoji", "emoji": "🕘"},
            "properties": {"title": {"title": [{"text": {"content": TIMETABLE_TITLE}}]}},
            "children": timetable_blocks(self.school),
        })
        self.log.append(f"ページを作成: {TIMETABLE_TITLE}")
        return page["id"]

    def _resolve(self, spec: dict) -> dict:
        """つながりの印を、相手の data_source_id を指す列にする。戻り側の印（_back）は作らないので外す。"""
        properties = {}
        for name, value in spec["properties"].items():
            relation = value.get("relation")
            if relation is None:
                properties[name] = value
            elif "back_of" in relation:
                continue
            else:
                target = self.state["databases"][relation["target"]]["data_source_id"]
                kind = "dual_property" if relation["synced"] else "single_property"
                detail = {"synced_property_name": relation["synced"]} if relation["synced"] else {}
                properties[name] = {"relation": {"data_source_id": target, "type": kind, kind: detail}}
        return {**spec, "properties": properties}

    def order_views(self, info: dict, spec: dict) -> None:
        """表のビューの列を spec の順にそろえる（戻り側の列も含む）。spec に無い列は消さず、右に残す。"""
        live = self.notion.request("GET", f"/data_sources/{info['data_source_id']}")["properties"]
        names = [n for n in spec["properties"] if n in live] + [n for n in live if n not in spec["properties"]]
        wanted = [{"property_id": live[n]["id"], "visible": True} for n in names]
        for view in self.notion.request("GET", f"/views?database_id={info['database_id']}").get("results", []):
            full = self.notion.request("GET", f"/views/{view['id']}")
            if full.get("type") != "table":
                continue
            shown = [(p.get("property_id"), p.get("visible"))
                     for p in (full.get("configuration") or {}).get("properties", [])]
            if shown == [(p["property_id"], p["visible"]) for p in wanted]:
                continue
            self.notion.request("PATCH", f"/views/{view['id']}",
                                {"configuration": {"type": "table", "properties": wanted}})
            self.log.append(f"列の順番をそろえた: {full.get('name') or view['id']}")

    def add_course(self, name: str, weekday: str, period: int | None = None,
                   term: str = "", year: int | None = None) -> None:
        # （今の 236-265 行目のまま。Task 2 で新しい列にする）
```

`read_seed` と `add_course` の本体は、今のファイルの同じ関数をそのまま写す（上のコメントの行は残さない）。`GPA`・`TITLE_ALIASES`・`CourseSetup.database` の上書きは無くなる。

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_course_setup.py tests/test_course_school.py tests/test_course_ics.py tests/test_academic_sync.py -v`
Expected: PASS（`tests/test_course_setup.py` は 9 件）

- [ ] **Step 7: 全体を確かめる**

Run: `uv run python -m pytest && uvx ruff check .`
Expected: すべて PASS（`tests/test_notion_gateway.py` も、戻り側の列が増えても通る）、ruff の指摘なし

- [ ] **Step 8: コミット**

```bash
git add modules/course/notion_setup.py modules/course/academic_record.py src/kei_agent/testing/fakes.py \
  tests/test_course_setup.py tests/test_course_school.py tests/test_course_ics.py tests/test_academic_sync.py
git commit -m "feat: define the course home with ordered English columns"
```

---

### Task 2: 課題と授業の読み書き（Moodle の取り込み・提出の確認・締切一覧）

**Files:**
- Modify: `modules/course/notion_sync.py:1-14,27-30,39,153-336,358-363`
- Modify: `modules/course/notion_setup.py` の `add_course`（Task 1 のあとの位置）
- Modify: `modules/course/submissions.py:8-11,47-49,57-59,73-76`
- Modify: `modules/course/agent.py:56-60,132-138`
- Modify: `src/kei_agent/operations/hands_server.py:116`
- Modify: `tests/test_course_sync.py:11-21,39-75,86-132,158-167,184-186,201-209,218-232`
- Modify: `tests/test_moodle_submissions.py:157-161,168-174,195,264,299`
- Modify: `tests/test_a2a.py:95-98`、`tests/test_catalog.py:19-20`、`tests/test_course_identity.py:15-16,46`、`tests/test_course_ics.py:218-268`

**Interfaces:**
- Consumes: Task 1 の `TAKING`・`DONE`・`NOT_STARTED`・`SUBMITTED`・`OVERDUE`
- Produces:
  - `notion_sync.REQUIRED_DATABASES = frozenset({"courses", "assignments", "grades", "requirements"})`
  - `CourseNotion.calendar_assignments(days: int, today: date) -> dict` の `items` の鍵は今と同じ（`id`・`title`・`due`・`status`・`url`・`course`・`moodle`・`moodle_id`）。`moodle` は課題の Link を返す
  - `CourseNotion.courses_on(weekday: str = "", on: date | None = None) -> list[dict]` の鍵は今と同じ（`id`・`subject`・`weekday`・`term`・`period`・`url`）
  - `CourseNotion._properties(event, course_id) -> dict` は `Name`・`Due`・`Moodle ID`・`Link`・`Course` だけを書く（出どころは書かない）
  - `agent.due_data(items, days, *, now=None) -> dict` は `status` が `Submitted`・`Overdue` の課題を除く

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_course_sync.py` を新しい列にする。11 行目の import の後ろに `from kei_agent_modules.course.notion_setup import DONE, NOT_STARTED, TAKING` を足し、`STATE`（16-21 行目）を次にする。

```python
STATE = {"home_page_id": "course-home", "databases": {
    "courses": {"data_source_id": "ds-courses"},
    "assignments": {"data_source_id": "ds-assignments"},
}}
```

`_row`・`FakeNotion`・`COURSE_ROWS`（39-75 行目）を次にする（`appended_children` はそのまま）。

```python
def _row(event, page_id="row-1", course_page="page-db", when=None, status=NOT_STARTED):
    return {"id": page_id, "url": f"https://notion.example/{page_id}", "properties": {
        "Name": _title(event.summary),
        "Status": {"status": {"name": status}},
        "Due": {"date": {"start": when or event.starts_at.astimezone().isoformat()}},
        "Course": {"relation": [{"id": course_page}]},
        "Link": {"url": event.url},
        "Moodle ID": _rich(event.uid),
    }}


class FakeNotion:
    def __init__(self, courses=(), assignments=(), page_children=None):
        self.rows = {"ds-courses": list(courses), "ds-assignments": list(assignments)}
        self.calls = []
        self.page_children = dict(page_children or {})

    def paginate(self, method, path, body=None):
        return self.rows[path.split("/")[2]]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/blocks/") and path.endswith("/children"):
            page_id = path.split("/")[2]
            return {"results": self.page_children.get(page_id, [])}
        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            page_id = path.split("/")[2]
            self.page_children.setdefault(page_id, []).extend(body["children"])
            return {"results": body["children"]}
        return {"id": "new-row"}

    def appended_children(self, page_id):
        return [block["heading_2"]["rich_text"][0]["text"]["content"]
                for block in self.page_children.get(page_id, []) if block.get("type") == "heading_2"]


COURSE_ROWS = [{"id": "page-db", "properties": {"Name": _title("データベース")}}]
```

`test_calendar_assignment_snapshot_reads_all_rows_with_course_link_and_clean_title` と `test_a_new_deadline_becomes_a_row_with_sections`（86-132 行目）を次にする。

```python
def test_calendar_assignment_snapshot_reads_all_rows_with_course_link_and_clean_title():
    """手入力の行も含めて全部読み（書き込みはしない）、締切のまとめ知らせ用に科目名・Link・言い回しを落とした課題名を返す。"""
    assignments = [_row(REPORT, page_id=f"p-{i}") for i in range(21)]
    assignments.append({"id": "manual", "url": "https://notion.example/manual", "properties": {
        "Name": _title("手入力の課題"), "Due": {"date": {"start": "2026-10-01T23:59:00+09:00"}},
        "Status": {"status": {"name": NOT_STARTED}},
    }})
    notion = FakeNotion(courses=COURSE_ROWS, assignments=assignments)

    snapshot = notion_sync.CourseNotion(notion, STATE).calendar_assignments(
        days=30, today=date(2026, 9, 24))

    assert snapshot["complete"] is True
    assert len(snapshot["items"]) == 22
    items = {item["id"]: item for item in snapshot["items"]}
    assert items["manual"] == {
        "id": "manual", "title": "手入力の課題", "due": "2026-10-01T23:59:00+09:00",
        "status": "Not started", "url": "https://notion.example/manual",
        "course": "", "moodle": "", "moodle_id": ""}
    assert (items["p-0"]["title"], items["p-0"]["course"], items["p-0"]["moodle"], items["p-0"]["moodle_id"]) == (
        "第3回レポート", "データベース", REPORT.url, REPORT.uid)
    assert notion.calls == []


def test_a_new_deadline_becomes_a_row_with_sections():
    """新しい締切は行になり、本文に見出しの型を入れる。出どころは書かない。すでに本文のあるページには足さない。"""
    notion = FakeNotion(courses=COURSE_ROWS, page_children={"new-row": []})
    result = _sync(notion, [REPORT])

    (method, path, body), = _page_writes(notion)
    assert (method, path) == ("POST", "/pages")
    props = body["properties"]
    assert set(props) == {"Name", "Status", "Due", "Course", "Link", "Moodle ID"}
    assert props["Name"]["title"][0]["text"]["content"] == "第3回レポート の 提出期限"
    # 手元の時刻に時差を付けて渡す（Notion 側でずれない）
    assert props["Due"]["date"]["start"] == REPORT.starts_at.astimezone().isoformat()
    assert props["Moodle ID"]["rich_text"][0]["text"]["content"] == REPORT.uid
    assert props["Link"] == {"url": REPORT.url}
    # 科目名は履修コードを外して「授業」と突き合わせる
    assert props["Course"]["relation"] == [{"id": "page-db"}]
    assert props["Status"]["status"]["name"] == "Not started"
    # 通知の行は Moodle の言い回しを落とし、締切の時刻を付ける
    assert result.added == ["`09/25 23:59` データベース / 第3回レポート"] and result.unchanged == 0
    assert notion.appended_children("new-row") == ["やること", "提出物", "進捗メモ", "資料・リンク"]

    notion.page_children["existing-row"] = [{"type": "paragraph"}]
    notion_sync.CourseNotion(notion, STATE).ensure_assignment_template("existing-row")
    assert notion.appended_children("existing-row") == []
```

`test_the_notification_label_moves_the_time_range` の `"科目名"` を `"Name"` にする。`test_a_moved_deadline_updates_the_same_row`（158-167 行目）を次にする。

```python
def test_a_moved_deadline_updates_the_same_row():
    """締切が変わったら、同じ行を直す（新しい行を作らない）。手で直した Status には触らない。"""
    old = _row(REPORT, when="2026-09-20T23:59:00+09:00", status="Submitted")
    notion = FakeNotion(courses=COURSE_ROWS, assignments=[old])
    result = _sync(notion, [REPORT])

    (method, path, body), = notion.calls
    assert (method, path) == ("PATCH", "/pages/row-1")
    assert "Status" not in body["properties"]
    assert set(body["properties"]) <= {"Name", "Due", "Course", "Link", "Moodle ID"}
    assert len(result.updated) == 1
```

`test_a_course_that_is_not_taken_is_skipped_unless_all_are_asked` の `assert "科目" not in body["properties"]`（185 行目）を `assert "Course" not in body["properties"]` にする。`test_missing_or_legacy_state_says_what_to_run`（201-209 行目）の後ろに足す。

```python
def test_a_state_without_the_gpa_database_is_enough(tmp_path):
    """GPA推移の DB は無くなった。授業・課題・成績・単位要件がそろっていれば読める。"""
    path = tmp_path / "notion-course.json"
    keys = ("courses", "assignments", "grades", "requirements")
    path.write_text(json.dumps({"databases": {key: {} for key in keys}}))
    assert set(notion_sync.read_state(path)["databases"]) == set(keys)
```

`_course`（218-226 行目）と `COURSE_ROWS_FULL`（229-234 行目）を次にする。

```python
def _course(key, name, day="月", period=2, status=TAKING, term="秋学期", year=None):
    props = {"Name": _title(name), "Day": _select(day), "Period": {"number": period}, "Status": _select(status)}
    if term:
        props["Term"] = _select(term)
    if year:
        props["Year"] = {"number": year}
    return {"id": key, "url": f"https://notion/{key}", "properties": props}


COURSE_ROWS_FULL = [
    _course("p1", "データベース"),
    _course("p2", "次世代ネットワーク", day="金", period=4),
    _course("p3", "去年の科目", period=1, status=DONE, term=None),
    _course("p4", "プロジェクト研究B", day="他", period=None),
]
```

`tests/test_moodle_submissions.py` を新しい列にし、締切一覧のテストを足す。import に `from kei_agent_modules.course.notion_setup import IN_PROGRESS, NOT_STARTED, OVERDUE, SUBMITTED` を足す。

```python
def quiz_row():
    row = _row(QUIZ)
    row["properties"]["Moodle ID"] = {"rich_text": [{"plain_text": "123@moodle.example/moodle"}]}
    row["properties"]["Status"] = {"status": {"name": IN_PROGRESS}}
    return row


def test_completed_quiz_updates_only_status_and_missing_link_once():
    row = quiz_row()
    client, notion = client_for(row)
    api = FakeAPI()
    first = submissions.sync(api=api, client=client)
    assert first.completed == ["Short test 1"] and first.checked == 1
    assert notion.calls == [("PATCH", "/pages/row-1", {"properties": {
        "Status": {"status": {"name": "Submitted"}},
        "Link": {"url": ROOT + "/mod/quiz/view.php?id=42"},
    }})]
    assert submissions.sync(api=api, client=client).completed == []
    assert len(notion.calls) == 1


@pytest.mark.parametrize("status", [SUBMITTED, OVERDUE])
def test_submitted_and_overdue_assignments_are_left_out_of_the_due_list(status):
    """手で Overdue にした課題や Submitted の課題は、Due がまだ先でも締切一覧に出さない。"""
    from kei_agent_modules.course.agent import due_data

    now = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=9)))
    rows = [{"id": "kept", "title": "レポート", "due": "2026-10-05T23:59:00+09:00", "status": NOT_STARTED},
            {"id": "dropped", "title": "手で閉じた課題", "due": "2026-10-04T23:59:00+09:00", "status": status}]
    assert [item["id"] for item in due_data(rows, 14, now=now)["items"]] == ["kept"]
```

同じファイルの残り3か所を直す。`test_manual_and_already_submitted_rows_are_not_checked` の `done["properties"]["状態"]["status"]["name"] = "提出済み"`（195 行目）を `done["properties"]["Status"]["status"]["name"] = SUBMITTED` に、`test_date_only_deadline_is_visible_through_that_day` の `"status": "未着手"`（264 行目）を `"status": NOT_STARTED` に、`test_partial_notion_failure_still_records_success_and_calendar_refresh` の最後の行（299 行目）を `assert second["properties"]["Status"]["status"]["name"] == IN_PROGRESS` にする。

`tests/test_a2a.py` の `test_asking_a_skill_comes_back_with_an_answer`（95-98 行目）の `"status": "進行中"` を `"status": "In progress"`、`"status": "提出済み"` を `"status": "Submitted"`、`"status": "未着手"` を `"status": "Not started"` にする（題の「提出済み」「期限切れ」は文字列なのでそのまま）。

`tests/test_catalog.py` の 19-20 行目の `"科目名"` を `"Name"` にする。`tests/test_course_identity.py` の `Notion.__init__`（15-16 行目）と 46 行目を次にする。

```python
        self.rows = [{"id": key, "properties": {"Name": {"title": [{"plain_text": name}]},
                                               **({"Status": {"select": {"name": status}}} if status else {})}}
                     for key, name, status in rows]
```

```python
    notion = Notion(("old", "情報セキュリティB", "Done"), ("current", "情報セキュリティB", "Taking"))
```

`tests/test_course_ics.py` の `test_add_course_writes_the_academic_year_and_the_seed_file`（218-268 行目）の `_Notion.paginate` の行を `{"Name": {"title": [{"plain_text": "データベース"}]}, "Year": {"number": 2025}}` に、`assert posts[0]["properties"]["年度"] == {"number": 2026}` を次の2行に、最後の行の `["学期"]` を `["Term"]` にする。

```python
    assert posts[0]["properties"]["Year"] == {"number": 2026}
    assert posts[0]["properties"]["Status"] == {"select": {"name": "Taking"}}
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_course_sync.py tests/test_moodle_submissions.py tests/test_a2a.py tests/test_catalog.py tests/test_course_identity.py tests/test_course_ics.py -v`
Expected: FAIL（`calendar_assignments` の title が空で `SyncError: 課題 ... の名前または URL がありません`、`props["出どころ"]` が残る、`read_state` が gpa の無い控えで SyncError、`due_data` が Overdue を残す、`add_course` が `年度` を書く など）

- [ ] **Step 3: 取り込みと授業の読み取りを新しい列にする**

`modules/course/notion_sync.py` の docstring（1-14 行目）を次にする。

```python
"""Moodle の締切を、Notion の「課題」に書き込む。

`kei-agent-module course setup` で作った授業ホームに、ics から読んだ締切を1行ずつ入れる。
同じ課題を二重に作らないよう、Moodle のイベント ID（ics の UID）を Moodle ID に入れて照合する。
書くのは Name・Due・Course・Link・Moodle ID と、新しい行の Status（Not started）だけ。手で直した Status には触らない。
取り込むのは「授業」に入れた履修科目の締切だけにする（Moodle のカレンダーには、
新入生向けの資料など、履修していない科目の締切も並ぶため）。

Notion はゲートウェイ経由（client は course）で、授業ホームの中だけに届く。

使い方（手で動かすとき。Notion ゲートウェイが動いていること）:
    source ~/.config/kei-agent/secrets/kei-agent.zsh   # 置き場所は config.toml の [paths] secrets
    kei-agent-module course sync
"""
```

import（27-30 行目）に `from .notion_setup import DONE, NOT_STARTED, TAKING` を足し、39 行目を `REQUIRED_DATABASES = frozenset({"courses", "assignments", "grades", "requirements"})` にする。`CourseNotion` の次のメソッドを置き換える。

```python
    def _course_names(self) -> dict[str, str]:
        """「授業」のページ ID → 科目名（表示用。normalize_course_name は通さない）。"""
        return {row["id"]: plain(row["properties"].get("Name")) for row in self._rows(self.courses)}

    def calendar_assignments(self, days: int, today: date) -> dict:
        """課題 DB の締切を全件読む。Moodle ICS の件数上限や同期は通さない。"""
        if not 1 <= days <= 400:
            raise SyncError("取得期間は1〜400日にしてください")
        through = today + timedelta(days=days - 1)
        rows = self.notion.paginate("POST", f"/data_sources/{self.assignments}/query", {
            "filter": {"and": [
                {"property": "Due", "date": {"on_or_after": today.isoformat()}},
                {"property": "Due", "date": {"on_or_before": through.isoformat()}},
            ]},
            "page_size": 100,
        })
        course_names = self._course_names()
        seen = set()
        items = []
        for row in rows:
            page_id = row.get("id")
            if not page_id or page_id in seen:
                raise SyncError("課題 DB のページ ID が欠落または重複しています")
            seen.add(page_id)
            props = row.get("properties") or {}
            due = (props.get("Due", {}).get("date") or {}).get("start")
            if not due:
                continue
            try:
                due_day = date.fromisoformat(due[:10])
            except ValueError:
                raise SyncError(f"課題 {page_id} の締切が不正です") from None
            if not today <= due_day <= through:
                continue
            title = plain(props.get("Name"))
            url = row.get("url")
            if not title or not url:
                raise SyncError(f"課題 {page_id} の名前または URL がありません")
            related = [r.get("id") for r in (props.get("Course") or {}).get("relation") or []]
            course = next((course_names[rid] for rid in related if rid in course_names), "")
            items.append({"id": page_id, "title": notice_title(title), "due": due,
                          "status": ((props.get("Status") or {}).get("status") or {}).get("name") or "",
                          "url": url, "course": course,
                          "moodle": (props.get("Link") or {}).get("url") or "",
                          "moodle_id": plain(props.get("Moodle ID"))})
        items.sort(key=lambda item: (item["due"], item["title"], item["id"]))
        return {"complete": True, "items": items}

    def course_ids(self) -> dict[str, str]:
        """科目名 → 「授業」のページ ID（Status が Done の授業は除く）。"""
        found: dict[str, str] = {}
        for row in self._rows(self.courses):
            if select(row["properties"].get("Status")) == DONE:
                continue
            name = normalize_course_name(plain(row["properties"].get("Name")))
            if not name:
                continue
            if name in found:
                raise SyncError(f"授業 DB に同じ科目が重複しています: {name}")
            found[name] = row["id"]
        return found

    def courses_on(self, weekday: str = "", on: date | None = None) -> list[dict]:
        """履修中（Status が Taking か空）の科目（曜日・時限つき）。weekday を渡すと、その曜日だけ。

        学期の終わった科目が残っていても混ざらないよう、その日の学期（と通年）だけを返す。
        年度が入っている科目は、その日の年度のものだけ（去年の Taking を今年に出さない）。
        """
        on = on or date.today()
        found = []
        for row in self._rows(self.courses):
            props = row["properties"]
            if select(props.get("Status")) not in ("", TAKING):
                continue
            if not self.school.in_term(select(props.get("Term")), on, number(props.get("Year"))):
                continue
            day = select(props.get("Day"))
            if weekday and day != weekday:
                continue
            found.append({
                "id": row["id"],
                "subject": plain(props.get("Name")),
                "weekday": day,
                "term": select(props.get("Term")),
                "period": (props.get("Period") or {}).get("number"),
                "url": row.get("url", ""),
            })
        found.sort(key=lambda c: (c["period"] is None, c["period"] or 0, c["subject"]))
        return found
```

`sync` の中の `props["状態"] = {"status": {"name": "未着手"}}` を `props["Status"] = {"status": {"name": NOT_STARTED}}` にし、`_properties` と `_differs` を次にする。

```python
    def _properties(self, event: Event, course_id: str | None) -> dict:
        props = {
            "Name": {"title": [{"text": {"content": assignment_title(event.summary)[:TITLE_LIMIT]}}]},
            # ics は手元の時刻に直してあるので、時差を付けて渡す（Notion 側でずれない）
            "Due": {"date": {"start": event.starts_at.astimezone().isoformat()}},
            "Moodle ID": {"rich_text": [{"text": {"content": event.uid}}]},
        }
        if event.url:
            props["Link"] = {"url": event.url}
        if course_id:
            props["Course"] = {"relation": [{"id": course_id}]}
        return props

    def _differs(self, row: dict, event: Event, course_id: str | None) -> bool:
        """Moodle 側と食い違っているか（Status は見ない）。"""
        props = row.get("properties") or {}
        if plain(props.get("Name")) != assignment_title(event.summary)[:TITLE_LIMIT]:
            return True
        when = _when(props.get("Due"))
        if when is None or when != event.starts_at.astimezone():
            return True
        if event.url and (props.get("Link") or {}).get("url") != event.url:
            return True
        related = [r.get("id") for r in (props.get("Course") or {}).get("relation") or []]
        return bool(course_id) and course_id not in related
```

`course_catalog`（358-363 行目）の `"科目名"` を `"Name"` にする。

- [ ] **Step 4: 授業を足すところを新しい列にする**

`modules/course/notion_setup.py` の `add_course` を次にする。

```python
    def add_course(self, name: str, weekday: str, period: int | None = None,
                   term: str = "", year: int | None = None) -> None:
        """科目を1つ足す（同じ年度・同じ名前があれば何もしない）。曜日と時限は別の列に入れる。

        年度を省くと今日の年度、学期を省くと今日の学期にする。年度が無いと、次の年も Taking の科目として出てしまう。
        """
        today = date.today()
        year = year or self.school.academic_year(today)
        term = term or self.school.term_of(today) or OTHER
        db = self.state["databases"]["courses"]
        for row in self.notion.paginate("POST", f"/data_sources/{db['data_source_id']}/query", {"page_size": 100}):
            props = row.get("properties", {})
            if notion_props.select(props.get("Status")) == DONE:
                continue
            if notion_props.number(props.get("Year")) not in (None, year):
                continue
            if normalize_course_name(notion_props.plain(props.get("Name"))) == normalize_course_name(name):
                return
        self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": db["data_source_id"]},
            "properties": {
                "Name": notion_props.title(name),
                "Status": {"select": {"name": TAKING}},
                "Year": {"number": year},
                "Term": {"select": {"name": term}},
                "Day": {"select": {"name": weekday}},
                "Period": {"number": period},
            },
        })
        self.log.append(f"科目を追加: {name}（{year}年度 {weekday}{period or ''}）")
```

- [ ] **Step 5: 提出の確認と締切一覧を新しい値にする**

`modules/course/submissions.py` の import に `from .notion_setup import SUBMITTED` を足し、`sync` の中を次のように直す。

```python
    pending = [row for row in rows if ((row.get("properties", {}).get("Status") or {}).get("status") or {}).get("name")
               != SUBMITTED]
```

```python
        title = notion_sync.notice_title(plain(props.get("Name")))
        url = (props.get("Link") or {}).get("url") or ""
```

```python
        if outcome.completed:
            changes = {"Status": {"status": {"name": SUBMITTED}}}
            if not url:
                changes["Link"] = {"url": outcome.url}
```

`modules/course/agent.py` の import に `from .notion_setup import OVERDUE, SUBMITTED` を足し、`due_data` の先頭を次にする。

```python
def due_data(items: list[dict], days: int, *, now: datetime | None = None) -> dict:
    """Notion の Status を使い、Submitted と Overdue の課題を締切一覧から除く。"""
    now = now or datetime.now().astimezone()
    pending = []
    for item in items:
        if item.get("status") in (SUBMITTED, OVERDUE):
            continue
```

同じファイルの `SYNC_SUBMISSIONS` の説明（58-59 行目）を `"認証付き Moodle API で課題の提出・小テストの受験終了を確認し、" "Notion の対応する課題の Status を Submitted にする。未提出・取得失敗では Status を戻さない"` にする。`src/kei_agent/operations/hands_server.py:116` の `Notion の課題を提出済みにする。` を `Notion の課題の Status を Submitted にする。` にする。

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_course_sync.py tests/test_moodle_submissions.py tests/test_a2a.py tests/test_catalog.py tests/test_course_identity.py tests/test_course_ics.py -v`
Expected: PASS

- [ ] **Step 7: 全体を確かめる**

Run: `uv run python -m pytest && uvx ruff check .`
Expected: すべて PASS、ruff の指摘なし

- [ ] **Step 8: コミット**

```bash
git add modules/course/notion_sync.py modules/course/notion_setup.py modules/course/submissions.py \
  modules/course/agent.py src/kei_agent/operations/hands_server.py tests/test_course_sync.py \
  tests/test_moodle_submissions.py tests/test_a2a.py tests/test_catalog.py tests/test_course_identity.py \
  tests/test_course_ics.py
git commit -m "feat: read and write assignments and courses with the new columns"
```

---

### Task 3: 成績と単位要件の取り込み

**Files:**
- Modify: `modules/course/academic_record.py:1-51`
- Modify: `modules/course/academic_sync.py:1-361`（全体）
- Modify: `modules/course/school.py:7-19,125-149`
- Modify: `modules/course/schools/waseda.py:1-166`
- Modify: `tests/test_academic_sync.py`（Task 1 で残した部分）
- Modify: `tests/test_academic_history.py`（全体）
- Modify: `tests/test_academic_record.py:37-43`
- Modify: `tests/test_course_school.py:66-71,79-85`

**Interfaces:**
- Consumes: Task 1 の `KIND_CATEGORY`・`KIND_SUBTOTAL`・`KIND_TOTAL`・`KIND_OTHER`・`KINDS`・`DONE`、Task 2 の `notion_sync.read_state`
- Produces:
  - `Requirement(name: str, group: str, required: float, included: float, remaining: float, kind: str)`（`earned` を消す）
  - `AcademicRecord(grades: tuple[Grade, ...], requirements: tuple[Requirement, ...])`（`gpa` と `GPAEntry` を消す）
  - `academic_sync.RECORD_ID = "Record ID"`
  - `AcademicImportResult(created: dict[str, int], updated: dict[str, int], unchanged: dict[str, int], ambiguous_relations: tuple[str, ...] = (), new_courses: tuple[str, ...] = ())`（dict の鍵は `"grades"`・`"requirements"`）
  - `AcademicSync(notion, state, school).sync(record: AcademicRecord) -> AcademicImportResult`
  - `requirement_changes(rows: dict[str, list[dict]], school: School) -> dict[str, dict]`、`link_requirements(notion, state: dict, school: School) -> int`
  - 消すもの: `gpa_key`・`academic_relation_changes`・`historical_course_changes`・`reconcile_academic_history`・`School.gpa_kind`・`School.gpa_label`・`School.course_group`・`School.course_groups`・`waseda.gpa_kind`・`waseda.GPA_KINDS`・`waseda.COURSE_GROUPS`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_academic_sync.py` の残り（`RowsNotion` から最後まで）を新しい形にする。import を次にする。

```python
import pytest

from kei_agent_modules.course import academic_sync, school
from kei_agent_modules.course.academic_record import AcademicRecord, Grade, Requirement
from kei_agent_modules.course.academic_sync import AcademicSync, grade_key
from kei_agent_modules.course.notion_props import plain

WASEDA = school.load({"school": "waseda"})
```

`RowsNotion.__init__` の `("courses", "grades", "requirements", "gpa")` を `("courses", "grades", "requirements")` にし、`STATE`・`course`・`record` を次にする。

```python
STATE = {"databases": {key: {"data_source_id": key} for key in ("courses", "grades", "requirements")}}
MATH = Grade("数学", 2025, "春期", 2, "A", 4, "基礎")


def course(page_id, name="数学", year=2025, term="春学期"):
    return {"id": page_id, "properties": {"Name": {"title": [{"plain_text": name}]}, "Year": {"number": year},
                                          "Term": {"select": {"name": term}}}}


def record(grades=(), requirements=()):
    return AcademicRecord(grades=tuple(grades), requirements=tuple(requirements))
```

`test_academic_sync_writes_existing_property_names` と `test_academic_sync_keeps_separate_grade_category_and_chronological_gpa_label` を消し、次を足す。

```python
def test_grades_and_requirements_use_the_new_columns_and_the_course_is_left_alone():
    notion = RowsNotion({"courses": [course("course-1")]})
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade("数学", 2025, "春期", 2, "A", 4, "Ｂ群 / 数学")], [Requirement("総合計", "", 124, 10, 114, "Total")]))

    grade = notion.rows["grades"][0]["properties"]
    assert set(grade) == {"Name", "Grade", "GP", "Credits", "Category", "Group", "Record ID", "Course"}
    assert (plain(grade["Group"]), plain(grade["Category"]), plain(grade["Grade"])) == ("Ｂ群", "数学", "A")
    assert grade["Course"] == {"relation": [{"id": "course-1"}]}
    requirement = notion.rows["requirements"][0]["properties"]
    assert set(requirement) == {"Name", "Remaining", "Required", "Counted", "Group", "Kind", "Record ID"}
    assert (requirement["Remaining"], requirement["Counted"]) == ({"number": 114}, {"number": 10})
    # 科目区分・科目群は成績にだけ書き、授業には書かない
    assert notion.rows["courses"] == [course("course-1")]


def test_a_requirement_kind_outside_the_options_is_written_as_other():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(requirements=[
        Requirement("総合計", "", 124, 10, 114, "Total"), Requirement("自由科目", "", 0, 2, 0, "区分")]))

    assert [row["properties"]["Kind"] for row in notion.rows["requirements"]] == [
        {"select": {"name": "Total"}}, {"select": {"name": "Other"}}]


def test_a_grade_without_its_course_creates_a_done_course_once():
    """「授業」に無い科目の成績は、終わった授業を1度だけ作って結ぶ。取り込み直しても2行にしない。"""
    notion = RowsNotion()
    sync = AcademicSync(notion, STATE, WASEDA)

    first = sync.sync(record([MATH]))
    second = sync.sync(record([MATH]))

    new_course, = notion.rows["courses"]
    assert new_course["properties"]["Status"] == {"select": {"name": "Done"}}
    assert (new_course["properties"]["Year"], new_course["properties"]["Term"]) == (
        {"number": 2025}, {"select": {"name": "春学期"}})
    assert "Category" not in new_course["properties"] and "Group" not in new_course["properties"]
    assert first.new_courses == ("数学 / 2025 / 春学期",) and second.new_courses == ()
    grade, = notion.rows["grades"]
    assert grade["properties"]["Course"] == {"relation": [{"id": new_course["id"]}]}
    assert second.unchanged == {"grades": 1, "requirements": 0}
```

残りのテストを次のように直す。

```python
def test_academic_sync_upserts_same_record_without_duplicate_pages():
    """2回目の同期は、同じ行を作らず、変わった要件だけを決まった鍵で書き直す。"""
    notion = RowsNotion()
    sync = AcademicSync(notion, STATE, WASEDA)
    first = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 114, "Total")]))
    second = sync.sync(record([MATH], [Requirement("総合計", "", 124, 10, 114, "Total")]))
    assert first.created == {"grades": 1, "requirements": 1}
    assert second.created == {"grades": 0, "requirements": 0}

    updated = sync.sync(record(requirements=[Requirement("総合計", "", 124, 12, 112, "Total")]))
    assert updated.updated == {"grades": 0, "requirements": 1}
    assert updated.created == {"grades": 0, "requirements": 0}


def test_ambiguous_course_is_reported_without_grade_relation():
    notion = RowsNotion({"courses": [course("math-a"), course("math-b")]})
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.ambiguous_relations == ("成績: 数学 / 2025 / 春期",)
    assert "Course" not in notion.rows["grades"][0]["properties"]
    assert len(notion.rows["courses"]) == 2


def test_academic_sync_preserves_an_existing_grade_course_relation():
    notion = RowsNotion({
        "courses": [course("matched-course")],
        "grades": [{"id": "grade-1", "properties": {
            "Name": {"title": [{"plain_text": "数学"}]},
            "Record ID": {"rich_text": [{"plain_text": "grade:2025:春期:数学"}]},
            "Credits": {"number": 2}, "Grade": {"rich_text": [{"plain_text": "A"}]},
            "GP": {"number": 4}, "Group": {"rich_text": [{"plain_text": "基礎"}]},
            "Category": {"rich_text": []},
            "Course": {"relation": [{"id": "manually-linked-course"}]},
        }}],
    }, writable=False)
    result = AcademicSync(notion, STATE, WASEDA).sync(record([MATH]))

    assert result.unchanged == {"grades": 1, "requirements": 0}
```

CLI の2つのテストの `AcademicRecord(grades=(), requirements=(), gpa=())` を `AcademicRecord(grades=(), requirements=())` に、`test_duplicate_record_keys_stop_before_any_write` の `"Kei Agent 成績ID"` を `"Record ID"` にする。`test_each_database_is_read_once_per_run` と `test_grade_matches_a_course_whose_name_differs_only_in_width` を次にする。

```python
def test_each_database_is_read_once_per_run():
    notion = RowsNotion()
    AcademicSync(notion, STATE, WASEDA).sync(record(
        [Grade(f"科目{i}", 2025, "春期", 2, "A", 4, "基礎") for i in range(5)],
        [Requirement(f"要件{i}", "", 2, 2, 0, "Category") for i in range(3)],
    ))

    assert sorted(notion.reads) == ["courses", "grades", "requirements"]


def test_grade_matches_a_course_whose_name_differs_only_in_width():
    """全角・半角の違いだけの科目名は、同じ科目として結ぶ（授業を作らない）。"""
    notion = RowsNotion({"courses": [course("course-b", "情報セキュリティB", term="秋学期")]})

    AcademicSync(notion, STATE, WASEDA).sync(record([Grade("情報セキュリティＢ", 2025, "秋期", 2, "A", 4, "基礎")]))

    (_method, _path, body), = notion.writes
    assert body["properties"]["Course"] == {"relation": [{"id": "course-b"}]}
```

`tests/test_academic_history.py` を次にする。

```python
"""成績を単位要件と結ぶところ（requirement_changes・link_requirements）。"""

import pytest

from kei_agent_modules.course import school
from kei_agent_modules.course.academic_sync import link_requirements, requirement_changes

# 単位要件の名前の対応は、早稲田の部品のもの
WASEDA = school.load({"school": "waseda"})


def text(value):
    return {"rich_text": [{"plain_text": value}]}


def title(value):
    return {"title": [{"plain_text": value}]}


def requirement(page_id, group, name):
    return {"id": page_id, "properties": {"Group": text(group), "Name": title(name)}}


def grade(page_id, group, category, linked=()):
    return {"id": page_id, "properties": {"Group": text(group), "Category": text(category),
                                          "Requirement": {"relation": list(linked)}}}


@pytest.mark.parametrize("linked, expected", [
    ([], {"grade": {"Requirement": {"relation": [{"id": "requirement"}]}}}),
    ([{"id": "manual"}], {}),   # 手で結んだ要件は置き換えない
])
def test_only_an_exact_requirement_is_linked_and_a_manual_link_is_kept(linked, expected):
    rows = {"grades": [grade("grade", "Ｂ群", "数学", linked)],
            "requirements": [requirement("requirement", "Ｂ群", "数学")]}
    assert requirement_changes(rows, WASEDA) == expected


def test_two_requirements_with_the_same_name_are_not_guessed():
    rows = {"grades": [grade("g", "Ｂ群", "数学")],
            "requirements": [requirement("r1", "Ｂ群", "数学"), requirement("r2", "Ｂ群", "数学")]}
    assert requirement_changes(rows, WASEDA) == {}


@pytest.mark.parametrize(("group", "category", "requirement_name"), [
    ("Ａ群", "外国語 英語", "外国語 英語 必修"),
    ("Ｂ群", "自然科学 物理学", "自然科学 物理学 必修"),
    ("Ｂ群", "自然科学 化学", "自然科学 化学 必修"),
    ("Ｃ群(専門教育科目)", "専門選択必修", "専門選択必修（学系別専門）"),
])
def test_verified_parent_requirement_links(group, category, requirement_name):
    rows = {"grades": [grade("g", group, category)], "requirements": [requirement("r", group, requirement_name)]}
    assert requirement_changes(rows, WASEDA) == {"g": {"Requirement": {"relation": [{"id": "r"}]}}}


def test_link_requirements_writes_only_the_grade_side():
    """成績の Requirement に書く。単位要件の Grades は Notion が戻り側として持つので書かない。"""
    class Notion:
        def __init__(self):
            self.writes = []

        def paginate(self, method, path, body=None):
            return {"grades": [grade("g", "Ｂ群", "数学")],
                    "requirements": [requirement("r", "Ｂ群", "数学")]}[path.split("/")[2]]

        def request(self, method, path, body=None):
            self.writes.append((method, path, body))
            return {}

    notion = Notion()
    state = {"databases": {"grades": {"data_source_id": "grades"},
                           "requirements": {"data_source_id": "requirements"}}}
    assert link_requirements(notion, state, WASEDA) == 1
    assert notion.writes == [("PATCH", "/pages/g", {"properties": {"Requirement": {"relation": [{"id": "r"}]}}})]
```

`tests/test_academic_record.py` の 37-43 行目を次にする。

```python
    record = waseda.read_record([grades, credits])

    grade, = record.grades
    assert (grade.course_name, grade.category, grade.gp) == ("数学", "Ａ群 / 基礎科目", 4.0)
    assert [(r.name, r.kind) for r in record.requirements] == [("専門必修", "Category"), ("総合計", "Total")]
    assert (record.requirements[-1].included, record.requirements[-1].remaining) == (15.0, 5.0)
    assert not hasattr(record, "gpa")
```

`tests/test_course_school.py` の自分の部品のテスト（66-71 行目）の `AcademicRecord((), (), ())` の2か所を `AcademicRecord((), ())` にし、`test_waseda_orders_gpa_by_term_and_links_grades_with_verified_names`（79-85 行目）を次にする。

```python
def test_waseda_turns_grade_terms_into_course_terms_and_links_grades_with_verified_names():
    waseda = school.load({"school": "waseda"})
    assert waseda.course_term("春期") == "春学期" and waseda.course_term("夏ク") == "夏ク"
    assert waseda.requirement_names("Ａ群", "外国語 英語") == ("外国語 英語", "外国語 英語 必修")
    assert not hasattr(waseda, "gpa_kind") and not hasattr(waseda, "course_group")
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_academic_sync.py tests/test_academic_history.py tests/test_academic_record.py tests/test_course_school.py -v`
Expected: FAIL（`ImportError: cannot import name 'link_requirements'`、`TypeError: AcademicRecord.__init__() missing 1 required positional argument: 'gpa'`、`Requirement` の引数の数、Kind が「総合計」のまま など）

- [ ] **Step 3: 記録の形から GPA と既得単位を外す**

`modules/course/academic_record.py` の docstring（1-5 行目）と `Requirement`・`GPAEntry`・`AcademicRecord`（28-51 行目）を次にする（Task 1 で足した Kind の定数はそのまま）。

```python
"""成績の記録の形（学校の部品の read_record が返す）と、保存した学務の HTML から表を拾う道具。

成績のページの読み方は学校ごとに違うので、学校の部品（schools/。書き方は school.py）に置く。ここには、どの学校でも
同じ記録の形（成績・単位要件）と、部品が使える HTML の表の読み方だけを置く。原文は残さない。
GPA と既得単位は持たない（GPA は Dot が成績から計算する）。
"""
```

```python
@dataclass(frozen=True)
class Requirement:
    name: str
    group: str
    required: float
    included: float
    remaining: float
    # KINDS のどれか（ほかの値は Other として書く）
    kind: str


@dataclass(frozen=True)
class AcademicRecord:
    grades: tuple[Grade, ...]
    requirements: tuple[Requirement, ...]
```

- [ ] **Step 4: 早稲田の部品と school.py から GPA と科目群の対応を消す**

`modules/course/schools/waseda.py` を次のように直す。

- docstring の 3 行目を「時限の時刻・学期の既定と、成績の取り込み方（保存した成績と単位の HTML を読む）。GPA は読まない。部品に置けるものは school.py に。」にする
- `import re` を消し、import を `from ..academic_record import KIND_CATEGORY, KIND_SUBTOTAL, KIND_TOTAL, AcademicRecord, Grade, Requirement, cell_number, cell_year, find_table` にする
- `_GPA_TERMS`・`GPA_KINDS`・`COURSE_GROUPS`（45-50 行目）、`gpa_kind`（73-78 行目）、`_gpa`（144-166 行目）を消す
- `read_record` の docstring を「成績の HTML（科目ごとの成績の表）と、単位の HTML（単位要件の表）を、この順に2つ読む。」にし、返り値を `AcademicRecord(_grades(grades_html), _requirements(credits_html))` にする
- `_requirements` の最後の3行を次にする

```python
        required, _earned, included = (value or 0 for value in numbers)
        kind = KIND_TOTAL if name == "総合計" else KIND_SUBTOTAL if "小計" in name else KIND_CATEGORY
        result.append(Requirement(name, group, required, included, max(required - included, 0), kind))
```

`modules/course/school.py` の docstring の部品の箇条（16-19 行目）を次の2行にし、`gpa_kind`・`gpa_label`（125-135 行目）と `course_group`・`course_groups`（142-149 行目）を消す。

```python
- `requirement_names(group, category)` … 成績の区分に当たる「単位要件」の名前の候補（無ければ区分の名前だけ）
- 単位要件の kind は academic_record.py の KINDS（Category / Subtotal / Total / Other）のどれかで返す
```

- [ ] **Step 5: 取り込みを書き換える**

`modules/course/academic_sync.py` を次にする。

```python
"""成績のファイルから作った学業記録を、授業ホームの成績・単位要件の DB に書き込む。

ファイルの読み方と、学校ごとの対応（成績の学期と授業 DB の Term、単位要件の名前）は学校の部品
（school.py）が持つ。ここは、どの学校でも同じ書き込み方だけを受け持つ。
行は照合キー（Record ID）で1度だけ作り、2回目からは差分だけを直す。成績に当たる授業が無ければ、
終わった授業（Status＝Done）として1度だけ作って結ぶ。科目区分・科目群は成績にだけ書く。GPA は保存しない。
各 DB は1回の実行で1度だけ読み、照合は手元の索引で行う。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from kei_agent_a2a.api import Notion, NotionError, gateway_notion, load_config

from .academic_record import KIND_OTHER, KINDS, AcademicRecord, Grade, Requirement
from .course_identity import normalize_course_name
from .notion_props import number, plain, select, text, title
from .notion_setup import DONE
from .notion_sync import read_state
from .school import School, from_config

_ACADEMIC_KEYS = ("grades", "requirements")
RECORD_ID = "Record ID"


@dataclass(frozen=True)
class AcademicImportResult:
    created: dict[str, int]
    updated: dict[str, int]
    unchanged: dict[str, int]
    ambiguous_relations: tuple[str, ...] = ()
    # 成績に当たる授業が無かったので作った授業（科目名 / 年度 / Term）
    new_courses: tuple[str, ...] = ()


def grade_key(grade: Grade) -> str:
    return f"grade:{grade.year}:{grade.term}:{grade.course_name}"


def requirement_key(requirement: Requirement) -> str:
    return f"requirement:{requirement.group}:{requirement.name}"


def _unique(kind: str, keys: list[str]) -> None:
    """同じ識別子が2つあれば、書き込む前に止める（後の行で前の行を黙って上書きしない）。"""
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise ValueError(f"{kind} の識別子が重複しています: {'、'.join(duplicates)}")


def _course_key(name: str, year: object, term: str, school: School) -> tuple[str, object, str]:
    return normalize_course_name(name), year, school.course_term(term)


def _split_category(category: str) -> tuple[str, str]:
    """「科目群 / 科目区分」を (Group, Category) に分ける。区分が無ければ Category は空。"""
    group, _, sub = category.partition(" / ")
    return group, sub


class AcademicSync:
    """派生済み AcademicRecord を Record ID で一度だけ記録する。"""

    def __init__(self, notion, state: dict, school: School):
        databases = state["databases"]
        self.notion = notion
        self.school = school
        self.sources = {key: databases[key]["data_source_id"] for key in ("courses", *_ACADEMIC_KEYS)}

    def _rows(self, key: str) -> list[dict]:
        return self.notion.paginate("POST", f"/data_sources/{self.sources[key]}/query", {"page_size": 100})

    def _index(self, key: str) -> dict[str, dict]:
        """Record ID → 既存の行。Notion 側で重複していたら、どちらを直すか決められないので止める。"""
        rows = [(plain(row.get("properties", {}).get(RECORD_ID)), row) for row in self._rows(key)]
        rows = [(identity, row) for identity, row in rows if identity]
        _unique(f"Notion の {key}", [identity for identity, _row in rows])
        return dict(rows)

    def _course_index(self) -> dict[tuple, list[str]]:
        """（科目名, 年度, Term）→ 授業のページ ID。"""
        index: dict[tuple, list[str]] = {}
        for row in self._rows("courses"):
            props = row.get("properties", {})
            key = _course_key(plain(props.get("Name")), number(props.get("Year")), select(props.get("Term")),
                              self.school)
            index.setdefault(key, []).append(row["id"])
        return index

    def _new_course(self, grade: Grade) -> str:
        """成績に当たる授業が無いとき、終わった授業として作る（科目区分・科目群は書かない）。"""
        page = self.notion.request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.sources["courses"]},
            "properties": {
                "Name": title(grade.course_name), "Status": {"select": {"name": DONE}},
                "Year": {"number": grade.year}, "Term": {"select": {"name": self.school.course_term(grade.term)}},
                "Credits": {"number": grade.credits},
            },
        })
        return page["id"]

    def _upsert(self, key: str, index: dict[str, dict], identity: str,
                properties: dict) -> tuple[str, tuple[str, ...]]:
        # （今の 89-112 行目のまま）

    @staticmethod
    def _properties_match(existing: dict, wanted: dict) -> bool:
        # （今の 114-128 行目のまま）

    def sync(self, record: AcademicRecord) -> AcademicImportResult:
        _unique("成績", [grade_key(grade) for grade in record.grades])
        _unique("単位要件", [requirement_key(item) for item in record.requirements])
        indexes = {key: self._index(key) for key in _ACADEMIC_KEYS}
        courses = self._course_index()
        counts = {outcome: dict.fromkeys(_ACADEMIC_KEYS, 0) for outcome in ("created", "updated", "unchanged")}
        ambiguous: list[str] = []
        new_courses: list[str] = []
        for grade in record.grades:
            identity = grade_key(grade)
            group, category = _split_category(grade.category)
            properties = {
                "Name": title(grade.course_name), "Grade": text(grade.grade), "GP": {"number": grade.gp},
                "Credits": {"number": grade.credits}, "Category": text(category), "Group": text(group),
                RECORD_ID: text(identity),
            }
            label = f"成績: {grade.course_name} / {grade.year} / {grade.term}"
            key = _course_key(grade.course_name, grade.year, grade.term, self.school)
            matches = courses.get(key, [])
            existing = indexes["grades"].get(identity) or {}
            linked = ((existing.get("properties") or {}).get("Course") or {}).get("relation")
            if len(matches) > 1:
                ambiguous.append(label)
            elif not matches and not linked:
                matches = courses[key] = [self._new_course(grade)]
                new_courses.append(f"{grade.course_name} / {grade.year} / {self.school.course_term(grade.term)}")
            if len(matches) == 1:
                properties["Course"] = {"relation": [{"id": matches[0]}]}
            outcome, preserved = self._upsert("grades", indexes["grades"], identity, properties)
            if preserved:
                ambiguous.append(f"{label}（既存の{'・'.join(preserved)} relation を保持）")
            counts[outcome]["grades"] += 1
        for requirement in record.requirements:
            identity = requirement_key(requirement)
            kind = requirement.kind if requirement.kind in KINDS else KIND_OTHER
            outcome, _ = self._upsert("requirements", indexes["requirements"], identity, {
                "Name": title(requirement.name), "Remaining": {"number": requirement.remaining},
                "Required": {"number": requirement.required}, "Counted": {"number": requirement.included},
                "Group": text(requirement.group), "Kind": {"select": {"name": kind}}, RECORD_ID: text(identity),
            })
            counts[outcome]["requirements"] += 1
        return AcademicImportResult(counts["created"], counts["updated"], counts["unchanged"],
                                    tuple(ambiguous), tuple(new_courses))


def requirement_changes(rows: dict[str, list[dict]], school: School) -> dict[str, dict]:
    """成績の Group・Category から単位要件が1つに決まるものだけを結ぶ。手で結んだつながりは残す。"""
    requirements: dict[tuple[str, str], list[str]] = {}
    for page in rows["requirements"]:
        props = page["properties"]
        requirements.setdefault((plain(props.get("Group")), plain(props.get("Name"))), []).append(page["id"])
    changes: dict[str, dict] = {}
    for page in rows["grades"]:
        props = page["properties"]
        group, category = plain(props.get("Group")), plain(props.get("Category"))
        if not group or not category or (props.get("Requirement") or {}).get("relation"):
            continue
        # 要件側の名前が成績側と違うものは、学校の部品が候補を足す
        target: list[str] = []
        for name in school.requirement_names(group, category):
            target = requirements.get((group, name), [])
            if target:
                break
        if len(target) == 1:
            changes[page["id"]] = {"Requirement": {"relation": [{"id": target[0]}]}}
    return changes


def link_requirements(notion: Notion, state: dict, school: School) -> int:
    """成績の Requirement に単位要件を結ぶ（単位要件の Grades は Notion が戻り側として持つ）。結んだ数を返す。"""
    rows = {key: notion.paginate("POST", f"/data_sources/{state['databases'][key]['data_source_id']}/query",
                                 {"page_size": 100})
            for key in _ACADEMIC_KEYS}
    changes = requirement_changes(rows, school)
    for page_id, properties in changes.items():
        notion.request("PATCH", f"/pages/{page_id}", {"properties": properties})
    return len(changes)


def main(argv: list[str] | None = None) -> int:
    """成績のファイルを学校の部品で読んで dry-run し、明示時だけ Notion へ書き込む入口。"""
    parser = argparse.ArgumentParser(
        prog="kei-agent-module course academic-import",
        description="成績のファイルを学校の部品（config.toml の [course] school）で読み、授業ホームに入れる")
    parser.add_argument("files", type=Path, nargs="+",
                        help="成績のファイル（早稲田は、成績の HTML と単位の HTML をこの順に2つ）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-inputs", action="store_true")
    args = parser.parse_args(argv)
    try:
        school = from_config(load_config())
        record = school.read_record(args.files)
    except (ValueError, OSError) as e:
        raise SystemExit(f"成績を読めません: {e}") from None
    print(f"成績 {len(record.grades)} 件・単位要件 {len(record.requirements)} 件")
    if args.dry_run:
        print("dry-run: Notion への書き込みは行いません")
        return 0
    if not args.apply:
        print("Notion へ反映するには --apply を付けてください")
        return 2
    try:
        notion = gateway_notion("course")
    except NotionError as e:
        raise SystemExit(str(e)) from None
    state = read_state()
    result = AcademicSync(notion, state, school).sync(record)
    links = link_requirements(notion, state, school)
    print("作成 " + "・".join(f"{key} {count} 件" for key, count in result.created.items()))
    print("更新 " + "・".join(f"{key} {count} 件" for key, count in result.updated.items()))
    print(f"単位要件とのつながり {links} 件")
    if result.new_courses:
        print("「授業」に終わった授業として足した: " + "、".join(result.new_courses))
    if result.ambiguous_relations:
        print("手で確認が必要な relation: " + "、".join(result.ambiguous_relations))
    total = sum(result.created.values()) + sum(result.updated.values()) + sum(result.unchanged.values())
    expected = len(record.grades) + len(record.requirements)
    if total != expected:
        raise RuntimeError(f"反映件数の検証に失敗しました: expected={expected}, actual={total}")
    if args.delete_inputs:
        for path in args.files:
            path.unlink()
        print("入力のファイルを削除しました")
    return 0
```

`_upsert` と `_properties_match` の本体は、今のファイルの同じメソッドをそのまま写す（上のコメントの行は残さない）。

- [ ] **Step 6: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_academic_sync.py tests/test_academic_history.py tests/test_academic_record.py tests/test_course_school.py -v`
Expected: PASS

- [ ] **Step 7: 残りが無いことと全体を確かめる**

Run: `git grep -n "gpa\|GPA\|既得単位\|COURSE_GROUPS\|course_group" -- modules/course tests`
Expected: `modules/course/academic_sync.py` と `academic_record.py` の docstring の「GPA は保存しない」「GPA と既得単位は持たない」、`notion_setup.py` の `GRADES` の説明、テストの `not hasattr(record, "gpa")`（`tests/test_academic_record.py`）と `not hasattr(waseda, "gpa_kind") and not hasattr(waseda, "course_group")`（`tests/test_course_school.py`）の行だけが出る

Run: `uv run python -m pytest && uvx ruff check .`
Expected: すべて PASS、ruff の指摘なし

- [ ] **Step 8: コミット**

```bash
git add modules/course/academic_record.py modules/course/academic_sync.py modules/course/school.py \
  modules/course/schools/waseda.py tests/test_academic_sync.py tests/test_academic_history.py \
  tests/test_academic_record.py tests/test_course_school.py
git commit -m "feat: import grades and requirements into the new course columns"
```

---

### Task 4: 共通ホームの「今週のタスク」の授業課題

**Files:**
- Modify: `src/kei_agent/storage/notion_hub_setup.py:50-59,153-154,308-312`
- Modify: `tests/test_notion_hub.py:93`（と、新しいテスト）

**Interfaces:**
- Consumes: Task 1 の課題の列名（`Due`・`Status`）と値（`Submitted`）。コアは `modules/` を読み込めないので、文字列で書く
- Produces: `task_view_spec(due: str, done: str, status: str = "状態") -> dict`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_notion_hub.py` の 93 行目を `"assignments-ds": self.ds("assignments-ds", {"Name": "title", "Due": "date", "Status": "status"}),` にし、`test_old_views_are_widened_once_and_duplicate_views_stop_setup` の後ろに足す。

```python
def test_the_assignment_view_reads_the_new_course_columns(fake_notion, tmp_path):
    """授業課題の表は、課題の Due と Status（Submitted 以外）で絞り、Due の近い順に並べる。"""
    setup(fake_notion, tmp_path).run()

    view = next(view for view in fake_notion.views if view["name"] == "授業課題")
    assert view["filter"]["and"][1] == {"property": "Status", "status": {"does_not_equal": "Submitted"}}
    assert view["sorts"] == [{"property": "Due", "direction": "ascending"}]
```

- [ ] **Step 2: 失敗を確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py -v`
Expected: FAIL（授業課題の DB に「締切」が無いので、setup の各テストが NotionError で止まる）

- [ ] **Step 3: 表の絞り込みを新しい列にする**

`src/kei_agent/storage/notion_hub_setup.py` の `task_view_spec`（50-59 行目）を次にする。

```python
def task_view_spec(due: str, done: str, status: str = "状態") -> dict:
    """共通ホームの「今週のタスク」の表。締切が今週・来週のものと、期限切れで終わっていないものを、締切の近い順に。"""
    return {
        "filter": {"and": [
            {"or": [{"property": due, "date": {when: {}}} for when in ("past_year", "this_week", "next_week")]},
            {"property": status, "status": {"does_not_equal": done}},
        ]},
        "sorts": [{"property": due, "direction": "ascending"}],
    }
```

153-154 行目の授業課題の `required` を `required={"Due": "date", "Status": "status"}` にし、308-312 行目を次にする。

```python
        for name, source, due, status, done in (
            ("研究 Task", tasks, "期日", "状態", "完了"),
            # 授業ホームの「課題」の列と値（modules/course/notion_setup.py の ASSIGNMENTS）
            ("授業課題", assignments, "Due", "Status", "Submitted"),
        ):
            spec = task_view_spec(due, done, status)
```

研究のプランが先に入って「研究 Task」の行が変わっているときは、その行の値を残して、授業課題の行だけを上のようにする。

- [ ] **Step 4: テストが通ることを確かめる**

Run: `uv run python -m pytest tests/test_notion_hub.py tests/test_layers.py -v`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add src/kei_agent/storage/notion_hub_setup.py tests/test_notion_hub.py
git commit -m "feat: filter the weekly assignment view by the new course columns"
```

---

### Task 5: 文書

**Files:**
- Modify: `docs/agents/course-agent.md:22,58-66,71,75-79,108-112`

- [ ] **Step 1: 課題の実績時間を誰も読んでいないことを確かめる**

Run: `git grep -n "実績時間\|見積時間\|学習ログ\|出どころ" -- modules/course src/kei_agent/scheduling`
Expected: `modules/course/agent.py` の `name="実績時間の集計"`（Toggl の集計の名前）だけが出る。ほかに出たら、その箇所を時間記録（Toggl）から数える形に直してから進む

- [ ] **Step 2: 授業ホームの節を書き換える**

`docs/agents/course-agent.md` の「## 授業ホーム（Notion）」の表（58-66 行目）を次にする。

```markdown
## 授業ホーム（Notion）

上から順に置く。列の名前と選択肢は英語で、列は表の順（名前 → よく見る列 → 時間と数字 → つながりと照合キー）。表のビューも同じ順にそろえる。

| 置くもの | 列（左から） |
|---|---|
| 授業時間表（ページ） | 何限が何時から何時か。Dot は授業の時刻をここから換算する |
| 授業 | Name・Status（Taking / Done）・Year・Term・Day・Period・Credits・Moodle。成績とは Name＋Year＋Term で突き合わせる |
| 課題 | Name・Status（Not started / In progress / Submitted / Overdue）・Due・Course・Link・Moodle ID（空なら手入力の課題） |
| 成績 | Name・Grade・GP・Credits・Category（科目区分）・Group（科目群）・Course・Requirement・Record ID |
| 単位要件 | Name・Remaining・Required・Counted・Group（大区分）・Kind（Category / Subtotal / Total / Other）・Grades・Record ID |

- 取得済みかは Grade が F でないことで見る。GPA は保存せず、Dot が成績から計算する（GP×Credits の合計÷Credits の合計）。大学の公式の GPA と小数点以下がずれることがある
- 単位の残りは単位要件の Remaining（Kind が Total の行が卒業要件の全体）。既得単位は取り込まない
- 勉強時間は共通ホームの「時間記録」（領域＝大学）で持つ
- `setup` は、題の列が Name でない DB や「📊 成績履歴」が残るホームには何も書かずに止まる
```

- [ ] **Step 3: 残りの書き方を新しい値にする**

- 22 行目の「Notion の対応する課題を「提出済み」にする」を「Notion の対応する課題の Status を Submitted にする」にする
- 71 行目のコメントを `# 授業時間表のページと4つの DB をそろえ、履修科目を入れる` にする
- 78-79 行目の箇条の後ろに「- 成績に当たる授業が「授業」に無ければ、終わった授業（Status＝Done）として足して結ぶ。単位要件は成績の Requirement で結ぶ」を足す
- 108 行目の「「提出済み」にする」を「Status を Submitted にする」に、110 行目の「「提出済み」の行と手入力の課題は確認対象から外す」を「Status が Submitted の行と手入力の課題（Moodle ID が空）は確認対象から外す」に、112 行目を「- 締切一覧は Notion の Status を参照し、Submitted と Overdue を除く。Dot の締切通知も同じ Status を見る」にする

- [ ] **Step 4: 確かめる**

Run: `uv run python -m pytest tests/test_docs_contract.py -v && uvx ruff check .`
Expected: PASS（`#自動でしていること`・`#moodle-の提出受験終了の同期` への link は見出しを変えていないので通る）

- [ ] **Step 5: コミット**

```bash
git add docs/agents/course-agent.md
git commit -m "docs: describe the redesigned course home"
```

---

### Task 6: Dot のプロンプト

**Files:**
- Modify: `docs/prompts/dot-daily.md`、`docs/prompts/dot-deadlines.md`、`docs/prompts/dot-review.md`、`docs/prompts/dot-custom-instructions.md`
- 確かめるだけ: `docs/prompts/dot-calendar-sync.md`

**前提:** PR #24 が main に入っていること。`git merge main` で作業ブランチに取り込んでから始める。行番号は PR #24 のあとのもの（ずれていたら下の引用の文で探す）。研究ホームの「Task」などの部分は研究のプランが直すので、ここでは授業ホームの部分だけを変える。

- [ ] **Step 1: 朝の一覧と Daily を書き換える**

`docs/prompts/dot-daily.md` の ` ```text ` の中の「- 授業: …」の行を次にする。

```text
- 授業: 授業ホームの「授業」（collection://5bf4c0f0-42ef-422c-ad71-4267074c46d9）で Status が Taking、Day が今日の曜日の行。Period は授業ホームの「授業時間表」ページで時刻にする。時間表で分からなければ時限のまま「時刻未確認」と書く（🎓 時刻 Name）
```

「- 締切: …」の行の授業ホームの部分を「授業ホームの「課題」（collection://05cd00df-4ce4-49c4-9674-fb217c07331d）で Status が Submitted・Overdue 以外、Due が今日のもの（科目は Course のつながり先の Name）」にする（研究ホームの「Task」の部分と、末尾の「（⏰ 時刻 締切: 科目 題）」はそのまま）。「**確認待ち・期日・止まっているテーマ・返事待ち**」の行の「3日以内に期日や締切が来る Task と課題」の後ろに「（課題は Status が Submitted・Overdue 以外で Due が3日以内のもの）」を足す。

- [ ] **Step 2: 締切の知らせを書き換える**

`docs/prompts/dot-deadlines.md` の本文の1つ目と2つ目の文を次にする。

```text
授業ホームの「課題」（collection://05cd00df-4ce4-49c4-9674-fb217c07331d）で、Status が Overdue と Submitted のものを除き、Due が24時間以内のものと、Due が3日以内で Status が Not started のものを集める。
直近3日に #0-overview へ自分が出した締切の知らせと、科目（Course のつながり先の Name）・題（Name）・締切（Due）で照合し、新しく対象になった課題と締切が変わった課題だけを出す。
```

- [ ] **Step 3: 振り返りを書き換える**

`docs/prompts/dot-review.md` の「**📌 明日・明後日の締切** …」の行の授業ホームの部分を「授業ホームの「課題」（collection://05cd00df-4ce4-49c4-9674-fb217c07331d）で Due が明日・明後日のもの（Status が Submitted・Overdue のものは除く）」にする（「と Task の締切」はそのまま）。

- [ ] **Step 4: 継続指示を書き換える**

`docs/prompts/dot-custom-instructions.md` の「- #2-course: …」の行を次の3行にする。

```text
- #2-course: 大学。授業・課題・成績・単位要件は Notion、要項や過去問は Box のプラグインで直接読む。必要な更新も Notion に直接行う。学習時間は Notion の時間記録（領域＝大学）から集計する。course を run に渡さない
- #2-course で成績や GPA を聞かれたら、授業ホームの「成績」DB から計算して答える。GPA＝GP×Credits の合計÷Credits の合計（GP が空の行は入れない）。学期ごとは Course のつながり先の Year・Term で分ける。取得単位は Grade が F でない行の Credits。大学が出す公式の GPA と小数点以下がずれることがあると添える
- #2-course で単位の残りを聞かれたら、授業ホームの「単位要件」DB の Remaining を読む（Kind が Total の行が卒業要件の全体）
```

- [ ] **Step 5: カレンダーの同期に課題の列が無いことを確かめる**

Run: `grep -n "締切\|状態\|提出済み\|未着手" docs/prompts/dot-calendar-sync.md`
Expected: 授業ホームの「課題」の列を読む行が無い（課題を予定カレンダーへ写すのは Kei Agent の大学のモジュールで、Dot は「その出典以外の行（手入力・課題・他のカレンダー）には触らない」だけ）。あれば Step 1 と同じ列名に直す

- [ ] **Step 6: 古い値が残っていないことを確かめる**

Run: `grep -n "提出済み\|期限切れ\|未着手\|履修中\|成績履歴\|GPA推移\|学習ログ" docs/prompts/*.md`
Expected: 授業ホームの課題・授業の値としては出ない（研究ホームの「未着手」など、研究の部分は研究のプランが直す）

- [ ] **Step 7: 確かめて、コミット**

Run: `uv run python -m pytest tests/test_docs_contract.py -v`
Expected: PASS

```bash
git add docs/prompts
git commit -m "docs: point Dot prompts at the redesigned course home"
```

---

### Task 7: Notion の移し替えと切り替え（本物に触る。各段で利用者に確認）

**Files:** なし（Notion と Dot の設定の操作）

各段の「**確認**」では、何をどう変えるかを利用者に見せ、はっきり「よい」と返事をもらってから進む。返事が無いまま次の段に進まない。

- [ ] **Step 1: 今の形と中身を控える**

Notion の MCP（`notion-fetch`）で授業ホームの「授業」「課題」「📊 成績履歴」「🎓 単位要件」「📈 GPA推移」「学習ログ」の列と全行を読み、セッションの作業用フォルダ（リポジトリの外）に控える。DB ごとの行の数と、消す予定の列（Global Constraints の「消す列」）に値が入っている行の数を表にして利用者に見せる。課題の見積時間・実績時間、学習ログの記録に値が残っていれば、消えてよいか（時間記録へ写すか）をここで聞く。

- [ ] **Step 2: 授業時間表のページを置く（確認）**

学校の部品の時限の時刻（早稲田は 1限 08:50-10:30 〜 7限 20:45-22:25）を下書きにして利用者に見せ、時刻と書き方を直してもらう。**確認**のあと、Notion の MCP で授業ホームに「授業時間表」ページを作り、確かめた中身を書く。ページを授業ホームの先頭に動かせなければ、利用者に Notion の画面でいちばん上へ動かしてもらう。

- [ ] **Step 3: 列の名前と選択肢を変える（確認）**

Step 1 で読んだ実際の列名から、次の対応表を作って利用者に見せる。実際の列名がこの表と違う（題の列が「タイトル」「科目名」など）ときは、その名前で表を作り直す。**確認**のあと、Notion の MCP（`notion-update-data-source`）で名前を変える（列を作り直さず、同じ列の名前と選択肢の名前を変えるので、値はそのまま残る）。

| DB | 今の名前 → 新しい名前 |
|---|---|
| 授業 | 科目名→Name、状態→Status（選択肢 履修中→Taking、終了→Done）、年度→Year、学期→Term、曜日→Day、時限→Period、単位→Credits、Moodle はそのまま |
| 課題 | 課題（またはタイトル）→Name、状態→Status（選択肢 未着手→Not started、進行中→In progress、提出済み→Submitted、期限切れ→Overdue）、締切→Due、科目→Course、Moodle→Link、Moodle ID はそのまま |
| 📊 成績履歴 | 授業名（または科目名）→Name、成績→Grade、GP はそのまま、単位→Credits、科目区分→Category、科目群→Group、授業→Course、単位要件→Requirement、Kei Agent 成績ID→Record ID |
| 🎓 単位要件 | 要件名→Name、残り単位→Remaining、所定単位→Required、算入単位→Counted、大区分→Group、集計種別→Kind（選択肢 区分→Category、小計→Subtotal、総合計→Total、その他→Other）、算入成績→Grades、Kei Agent 要件ID→Record ID |

- 課題の Status は status 型で、API から選択肢の名前を変えられないことがある。断られたら、利用者に Notion の画面（列の設定 → 選択肢の編集）で4つの名前を変えてもらう
- 足りない列（授業の Credits など）は、Task 1〜3 の PR をマージしたあとの Step 6 の setup が足すので、ここでは作らない
- 列の並びは Step 6 の setup がビューでそろえる

- [ ] **Step 4: 科目区分・科目群を成績に写す（確認）**

成績の各行の Category・Group（成績履歴に元からあった科目区分・科目群）と、つながっている授業の科目区分・科目群を並べた表を作る。

- 成績側が空で授業側にある行は、授業側の値を写す
- 両方に値があって違う行（例: 成績「Ｃ群(専門教育科目)」、授業「C群」）は写さずに一覧にする。成績側は学務の表記のまま残す（単位要件とのつながりは学務の表記で探すため）
- 写す行・写さない行の数と一覧を利用者に見せ、**確認**のあと Notion の MCP で写す

- [ ] **Step 5: DB の名前を変える（確認）**

「📊 成績履歴」を「成績」に、「🎓 単位要件」を「単位要件」にする。**確認**のあと Notion の MCP で名前を変える。

- [ ] **Step 6: 写ったことを照らし合わせる**

Notion の MCP で4つの DB を全行読み直し、Step 1 の控えとページ ID で突き合わせる。DB ごとに行の数が同じこと、名前を変えた列の値（Status・Due・Grade・Remaining など）が古い値の対応どおりであることを表にして利用者に見せる。食い違いは1件ずつ挙げ、0件になるまで直す。

- [ ] **Step 7: コードを切り替える（確認）**

1. 利用者に Task 1〜6 の PR をマージしてもらう（launchd の Kei Agent は main を動かす）
2. **確認**のあと `uv run kei-agent-module course setup` を動かす。題の列が Name でない、または古い名前の DB が残っていれば何も書かずに止まるので、その DB を Step 3・5 で直してからやり直す。出力（足した列・列の順番をそろえたビュー）を利用者に見せる
3. `uv run kei-agent-module course sync` と `uv run kei-agent-module course sync-submissions` を動かし、課題の行が増えすぎていない（新しい課題の分だけ）こと、Status が Submitted に変わる行があることを確かめる
4. 成績の HTML を利用者が用意できれば、`uv run kei-agent-module course academic-import --dry-run <grades.html> <credits.html>` のあと、**確認**して `--apply` を付けて動かす。2回目の作成が「grades 0 件・requirements 0 件」になり、成績と単位要件が2行にならないことを確かめる

- [ ] **Step 8: Dot を切り替える（確認）**

Task 6 で変えたプロンプト（朝の一覧と Daily・締切の知らせ・振り返り・継続指示）の全文を利用者に見せ、**確認**のあと今日と同じやり方で Kei に送る（「要約・言い換え・追記をせず、全文をそのまま保存して」）。保存後の照合の返事を確かめる。`sync_submissions` の説明を変えたので、ChatGPT で MCP を作り直して新しい説明が見えるかを利用者に確かめてもらう。

- [ ] **Step 9: 消す列と DB を消す（確認のあと）**

次を一覧にして利用者に見せ、項目ごとに**確認**を取る。よいと言われたものだけを Notion の MCP で消す（DB はゴミ箱に入り、30日間は戻せる）。

- 授業: 科目コード・履修年次・科目区分・科目群
- 課題: 出どころ・見積時間・実績時間・最終同期
- 成績: 取得年度・学期・取得済・取り込み元・GPA推移
- 単位要件: 既得単位・対象授業・取り込み元
- DB: GPA推移・学習ログ
- 設計書に書かれていない列は消さずに聞く: 授業の必選区分、授業に残る戻り側のつながり（課題・成績履歴・学習ログ・単位要件・GPA推移）

`~/.local/state/kei-agent/notion-course.json` に残る `gpa` の鍵は、コードが読まないのでそのままにする。

- [ ] **Step 10: 翌日に確かめる**

- 切り替えた翌日の朝の一覧で、今日の授業と時刻、今日が締切の課題が出ること
- 締切の知らせが、同じ課題を繰り返さずに出ること
- Moodle の取り込みと提出の確認で、課題の行が2行にならず、Status が変わること
- `uv run python -m pytest` と `uvx ruff check .` が main で通ること

Slack と Notion で確かめ、結果を利用者に伝える。
