"""AI の返事から JSON を拾う（kei_agent.model_json）。振り分け・分類・担当のどれもが使う。"""

import json

import pytest

from kei_agent.model_json import json_list, json_object


def test_json_list_takes_the_outer_array_even_when_items_hold_arrays():
    text = '```json\n[{"subject": "朝会", "attendees": [{"name": "A"}]}]\n```\n出典 [1, 2]'
    assert json_list(text) == [{"subject": "朝会", "attendees": [{"name": "A"}]}]


def test_json_list_prefers_the_array_that_holds_the_items():
    assert json_list('注記 [これは説明] 本文 [{"subject": "朝会"}]') == [{"subject": "朝会"}]
    assert json_list('[{"subject": "朝会"}]\n\n出典 [1, 2]') == [{"subject": "朝会"}]
    assert json_list("予定はありません。[]") == []


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
