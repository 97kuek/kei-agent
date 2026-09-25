"""大学エージェントの Agent Card（何ができるかを書いた名刺）。形は `kei_agent_a2a.card`。"""

from __future__ import annotations

from a2a.types import AgentCard, AgentSkill

from kei_agent_a2a.card import agent_card
from kei_agent_course.skills import (
    ASK,
    LIST_CALENDAR_ASSIGNMENTS,
    LIST_CLASSES,
    LIST_CURRENT_COURSES,
    LIST_DUE,
    RECORD_STUDY_TIME,
    SYNC_ASSIGNMENTS,
    TIME_REPORT,
)


def build_card(base_url: str) -> AgentCard:
    """このエージェントの名刺を作る。base_url は `http://127.0.0.1:8787` のような、外から見える住所。"""
    return agent_card(
        "Kei Agent（大学）",
        "Moodle の課題、Notion の授業と課題、Toggl の実績、Box の学部要項と過去問を扱う。"
        "自由な質問には、自分の claude が Box と Notion を読んで答える",
        base_url,
        output_modes=("text/plain", "application/json"),
        skills=[
            AgentSkill(
                id=SYNC_ASSIGNMENTS,
                name="課題を取り込む",
                description="Moodle から履修中の科目と課題を読み、Notion の授業・課題データベースに反映する。"
                            "新しく増えた課題と、締切が変わった課題を返す",
                tags=["moodle", "notion"],
                examples=["課題を取り込んで", "Moodle を見てきて"],
            ),
            AgentSkill(
                id=LIST_DUE,
                name="締切の近い課題",
                description="締切が近い順に JSON で返す（items: id/at/course/title/url）。"
                            "既定では2週間先まで。metadata の days で変えられる",
                tags=["moodle"],
                examples=["今週の締切は？", "明日までの課題を教えて"],
            ),
            AgentSkill(
                id=LIST_CALENDAR_ASSIGNMENTS,
                name="課題カレンダー用の全件取得",
                description="授業ホームの課題 DB から指定期間の締切を省略せず読み取る。"
                            "data.complete/items: id/title/due/status/url。書き込みはしない",
                tags=["notion", "calendar", "read-only"],
                examples=[],
            ),
            AgentSkill(
                id=LIST_CLASSES,
                name="その日の授業",
                description="履修中の科目のうち、その曜日のものを時刻つきで JSON で返す"
                            "（data.items: subject/weekday/period/start/end）。"
                            "既定は今日。metadata の weekday（月〜日）で変えられる",
                tags=["notion"],
                examples=["今日の授業は？", "金曜の時間割"],
            ),
            AgentSkill(
                id=LIST_CURRENT_COURSES,
                name="今学期の履修科目",
                description="今学期に履修中の科目だけを JSON で返す（items: id/subject/weekday/period）",
                tags=["notion", "course"],
                examples=["履修中の授業", "今学期の科目"],
            ),
            AgentSkill(
                id=RECORD_STUDY_TIME,
                name="学習時間を記録する",
                description="大学ホームの学習ログに、Kei Agent の確定済み時間を記録する",
                tags=["notion", "time"], examples=[],
            ),
            AgentSkill(
                id=ASK,
                name="授業のことに答える",
                description="定型に当てはまらない質問に、自分の claude が答える。Box の学部要項・過去問と、"
                            "Notion の授業・課題を読んで、根拠（ファイル名と URL）を付けて返す",
                tags=["box", "notion", "claude"],
                examples=["情報セキュリティBの過去問ある？", "卒業に必要な単位数は？", "この課題の出し方どうだった？"],
            ),
            AgentSkill(
                id=TIME_REPORT,
                name="実績時間の集計",
                description="Toggl の記録を科目ごと・課題ごとに集計して返す（読むだけ）。"
                            "既定は直近7日。metadata の days で変えられる",
                tags=["toggl"],
                examples=["今週、どの授業に何時間使った？", "先週の実績を見せて"],
            ),
        ],
    )
