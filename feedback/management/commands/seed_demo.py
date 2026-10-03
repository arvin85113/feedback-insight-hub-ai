from django.core.management.base import BaseCommand

from feedback.seed_support import (
    choice_question,
    create_published_survey,
    reset_survey,
    scale_question,
    text_question,
)

SLUG = "product-feedback"


class Command(BaseCommand):
    help = "建立專題展示用的示範問卷（建立後發布）；問卷已存在時需加 --reset 重建"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="先清除既有示範問卷再重建")

    def handle(self, *args, **options):
        if options["reset"]:
            reset_survey(SLUG)
        survey = create_published_survey(
            slug=SLUG,
            title="產品體驗回饋調查",
            description="蒐集使用體驗、滿意度與改進建議，供後續統計分析與產品優化。",
            questions=[
                scale_question("整體滿意度（1-10）", 1, 10, low_label="非常不滿意", high_label="非常滿意"),
                choice_question("您最常使用的平台功能是什麼？", ["統計圖表", "表單填寫", "通知追蹤", "資料匯出"]),
                choice_question("回應速度是否符合期待？", ["不符合", "普通", "符合", "非常符合"], ordered=True),
                text_question("請描述您最希望優先改善的地方", long=True, tracked=True),
            ],
            keywords=[("速度", "效能"), ("介面", "UI/UX"), ("通知", "追蹤機制"), ("圖表", "分析呈現")],
        )
        self.stdout.write(self.style.SUCCESS(f"已建立並發布示範問卷：{survey.title}"))
