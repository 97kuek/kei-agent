"""AI の返事から JSON を拾う（kei_agent.model_json）。振り分け・分類・担当のどれもが使う。"""

import json

import pytest

from kei_agent.model_json import json_list, json_object


@pytest.mark.parametrize(("text", "expected"), [
    ('```json\n[{"subject": "朝会", "attendees": [{"name": "A"}]}]\n```\n出典 [1, 2]',
     [{"subject": "朝会", "attendees": [{"name": "A"}]}]),                 # 中に配列があっても外側を取る
    ('はい、調べました。\n[{"subject": "定例"}]', [{"subject": "定例"}]),  # 前置きは読み飛ばす
    ('注記 [これは説明] 本文 [{"subject": "朝会"}]', [{"subject": "朝会"}]),  # 項目を持つ配列を選ぶ
    ('[{"subject": "朝会"}]\n\n出典 [1, 2]', [{"subject": "朝会"}]),
    ("予定はありません。[]", []),
])
def test_json_list_takes_the_array_that_holds_the_items(text, expected):
    assert json_list(text) == expected


def test_json_list_says_when_the_reply_is_not_json():
    with pytest.raises(ValueError, match="読めません"):
        json_list("[これは JSON ではない]")
    with pytest.raises(ValueError, match="JSON の配列"):
        json_list("予定はありません")


def test_json_list_reads_a_long_reply_without_trying_every_bracket_pair():
    noise = "[注] " * 3000
    items = [{"subject": f"会議{i}"} for i in range(300)]
    assert json_list(noise + json.dumps(items, ensure_ascii=False)) == items


def test_json_object_tolerates_code_fences_and_preambles():
    text = 'こちらです。\n```json\n{"complete": true, "items": []}\n```'
    assert json_object(text, "items") == {"complete": True, "items": []}
    with pytest.raises(ValueError):
        json_object("予定はありません", "items")


def test_json_object_needs_the_key_and_skips_broken_or_other_json():
    assert json_object('はい\n{"skill": "ask"}\nどうぞ', "skill") == {"skill": "ask"}
    assert json_object('{"note": 1} {"skill": "ask", "days": {"n": 3}}', "skill") == {"skill": "ask", "days": {"n": 3}}
    for text in ("よく分かりません", "{broken}", "[1, 2]", '{"note": 1}'):
        with pytest.raises(ValueError):
            json_object(text, "skill")
