from django.core.management.base import BaseCommand

from feedback.models import KeywordCategory, Question, Survey


class Command(BaseCommand):
    help = "建立專題展示用的示範問卷資料"

    def handle(self, *args, **options):
        survey, created = Survey.objects.get_or_create(
            slug="product-feedback",
            defaults={
                "title": "產品體驗回饋調查",
                "description": "蒐集使用體驗、滿意度與改進建議，供後續統計分析與產品優化。",
            },
        )
        questions = [
            {
                "order": 1,
                "title": "整體滿意度（1-10）",
                "kind": Question.Kind.SCALE,
                "data_type": Question.DataType.CONTINUOUS,
                "help_text": "1 代表非常不滿意，10 代表非常滿意",
                "options_text": "1\n2\n3\n4\n5\n6\n7\n8\n9\n10",
            },
            {
                "order": 2,
                "title": "您最常使用的平台功能是什麼？",
                "kind": Question.Kind.SINGLE_CHOICE,
                "data_type": Question.DataType.NOMINAL,
                "options_text": "統計圖表\n表單填寫\n通知追蹤\n資料匯出",
            },
            {
                "order": 3,
                "title": "回應速度是否符合期待？",
                "kind": Question.Kind.SINGLE_CHOICE,
                "data_type": Question.DataType.ORDINAL,
                "options_text": "非常符合\n符合\n普通\n不符合",
            },
            {
                "order": 4,
                "title": "請描述您最希望優先改善的地方",
                "kind": Question.Kind.LONG_TEXT,
                "data_type": Question.DataType.TEXT,
                "enable_keyword_tracking": True,
            },
        ]
        for question in questions:
            Question.objects.update_or_create(survey=survey, order=question["order"], defaults=question)

        for keyword, category in [
            ("速度", "效能"),
            ("介面", "UI/UX"),
            ("通知", "追蹤機制"),
            ("圖表", "分析呈現"),
        ]:
            KeywordCategory.objects.update_or_create(
                survey=survey,
                keyword=keyword,
                defaults={"category": category, "threshold": 2},
            )

        message = "已建立" if created else "已更新"
        self.stdout.write(self.style.SUCCESS(f"{message}示範問卷：{survey.title}"))
