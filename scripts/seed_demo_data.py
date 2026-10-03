"""Cross-department demo survey (ANOVA / t-test showcase), created and published through the builder path.

Writes to the database selected by Django settings: confirm the target before running (AGENTS.md).
"""

import os
import random
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from feedback.local_service import submit_survey_payload  # noqa: E402
from feedback.seed_support import (  # noqa: E402
    answers_by_title,
    choice_question,
    create_published_survey,
    number_question,
    reset_survey,
)

SLUG = "demo-2026-q1"


def generate_perfect_demo_data():
    print("[1/3] 清除舊有 Demo 問卷...")
    reset_survey(SLUG)

    print("[2/3] 建立並發布 Demo 問卷...")
    survey = create_published_survey(
        slug=SLUG,
        title="2026 Q1 跨部門系統體驗調查",
        description="本問卷專為展示自動化統計推論引擎（ANOVA / t-test）所設計。",
        questions=[
            choice_question("您的所屬單位", ["研發部", "行銷部", "業務部"]),
            number_question("系統整體流暢度評分（1-10分）", decimal=True),
        ],
    )

    print("[3/3] 灌入回覆資料...")
    strategies = {"研發部": (7, 10), "行銷部": (2, 5), "業務部": (5, 8)}
    for dept, (lo, hi) in strategies.items():
        for i in range(15):
            submit_survey_payload(
                survey,
                user=None,
                respondent_name=f"{dept}測試員_{i + 1}",
                respondent_email="",
                consent_follow_up=True,
                answers=answers_by_title(survey, {
                    "您的所屬單位": dept,
                    "系統整體流暢度評分（1-10分）": str(random.randint(lo, hi)),
                }),
            )

    print(f"[OK] 完成！共建立 {survey.submissions.count()} 筆回覆")
    print("     前往統計分析選擇「2026 Q1 跨部門系統體驗調查」查看結果")


if __name__ == "__main__":
    generate_perfect_demo_data()
