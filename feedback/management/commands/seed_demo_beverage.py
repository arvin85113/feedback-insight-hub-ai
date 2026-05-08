import random

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from feedback.local_service import submit_survey_payload
from feedback.models import (
    FeedbackSubmission,
    KeywordCategory,
    Question,
    Survey,
    SurveyCategory,
)

SURVEY_SLUG = "beverage-feedback"
SURVEY_TITLE = "飲料店體驗回饋"
SURVEY_DESCRIPTION = "蒐集顧客在不同門市的飲料體驗，作為服務優化與品項調整參考。"
CATEGORY_NAME = "飲料店"
NAME_PREFIX = "飲料店模擬填答"

STORES = ["信義店", "台北車站店", "公館店", "士林店"]
ITEMS = ["珍珠奶茶", "紅茶", "綠茶", "水果茶", "咖啡", "奶蓋茶"]
WAIT_LEVELS = ["很快", "普通", "久", "很久"]

QUESTIONS = [
    {
        "order": 1,
        "title": "您最常前往的門市",
        "kind": Question.Kind.SINGLE_CHOICE,
        "data_type": Question.DataType.NOMINAL,
        "options_text": "\n".join(STORES),
        "is_required": True,
        "enable_keyword_tracking": False,
    },
    {
        "order": 2,
        "title": "最常購買的品項（可複選）",
        "kind": Question.Kind.MULTIPLE_CHOICE,
        "data_type": Question.DataType.NOMINAL,
        "options_text": "\n".join(ITEMS),
        "is_required": True,
        "enable_keyword_tracking": False,
    },
    {
        "order": 3,
        "title": "整體滿意度（1-10）",
        "kind": Question.Kind.SCALE,
        "data_type": Question.DataType.CONTINUOUS,
        "options_text": "",
        "is_required": True,
        "enable_keyword_tracking": False,
        "help_text": "1 代表非常不滿意，10 代表非常滿意",
    },
    {
        "order": 4,
        "title": "甜度與口味滿意度（1-10）",
        "kind": Question.Kind.SCALE,
        "data_type": Question.DataType.CONTINUOUS,
        "options_text": "",
        "is_required": True,
        "enable_keyword_tracking": False,
    },
    {
        "order": 5,
        "title": "等候時間感受",
        "kind": Question.Kind.SINGLE_CHOICE,
        "data_type": Question.DataType.ORDINAL,
        "options_text": "\n".join(WAIT_LEVELS),
        "is_required": True,
        "enable_keyword_tracking": False,
    },
    {
        "order": 6,
        "title": "希望改善的地方",
        "kind": Question.Kind.LONG_TEXT,
        "data_type": Question.DataType.TEXT,
        "options_text": "",
        "is_required": False,
        "enable_keyword_tracking": True,
    },
]

KEYWORDS = [
    ("甜度", "口味"),
    ("冰塊", "口味"),
    ("服務", "服務品質"),
    ("等候", "服務品質"),
    ("價格", "價格"),
    ("環境", "環境"),
]

# 刻意讓不同門市的滿意度／等候時間呈現分布差異，
# 以利統計推論（ANOVA／Kruskal-Wallis／chi-square）出現顯著結果。
STORE_PROFILES = {
    "信義店": {
        "score_range": (8, 10),
        "sweet_range": (7, 10),
        "wait_weights": [0.6, 0.3, 0.1, 0.0],
        "item_pool": ["珍珠奶茶", "咖啡", "奶蓋茶"],
        "item_count": (1, 2),
        "text_pool": [
            "服務態度很好，飲料品質穩定，環境舒適。",
            "珍珠Q彈，咖啡香氣不錯，整體很滿意。",
            "店員推薦很實用，外帶速度很快，會再回購。",
            "甜度可以客製化，價格合理，環境乾淨。",
        ],
    },
    "台北車站店": {
        "score_range": (4, 7),
        "sweet_range": (5, 8),
        "wait_weights": [0.0, 0.2, 0.5, 0.3],
        "item_pool": ["紅茶", "綠茶", "珍珠奶茶"],
        "item_count": (1, 2),
        "text_pool": [
            "尖峰時段等候很久，希望能加開人手。",
            "排隊人潮多，櫃台數量太少，點餐到拿飲料花太久。",
            "服務速度慢，但飲料口味還算穩定。",
            "等候時間是最大問題，建議優化動線。",
        ],
    },
    "公館店": {
        "score_range": (6, 9),
        "sweet_range": (6, 9),
        "wait_weights": [0.2, 0.5, 0.25, 0.05],
        "item_pool": ["水果茶", "奶蓋茶", "綠茶"],
        "item_count": (1, 3),
        "text_pool": [
            "整體還不錯，希望甜度可以再客製化。",
            "冰塊量希望可以選擇，環境舒適。",
            "新品上架速度可以更快，水果茶口感很好。",
            "服務態度親切，價格合理。",
        ],
    },
    "士林店": {
        "score_range": (3, 6),
        "sweet_range": (3, 6),
        "wait_weights": [0.0, 0.2, 0.5, 0.3],
        "item_pool": ITEMS,
        "item_count": (1, 3),
        "text_pool": [
            "價格偏高，份量希望調整。",
            "服務態度可以再改善，環境嘈雜。",
            "等候時間長，座位不夠用。",
            "甜度偏高，希望提供無糖選項，店員應對需要訓練。",
        ],
    },
}


class Command(BaseCommand):
    help = (
        "建立飲料店示範問卷（6 題、6 條關鍵字分類）並灌入 100 筆模擬填答。"
        "支援 --cleanup（僅清填答）與 --reset（連同問卷整份重建）。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=100, help="模擬填答筆數（預設 100）")
        parser.add_argument("--seed", type=int, default=None, help="隨機種子，便於重現同一組假資料")
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help="只清除本指令灌入的模擬填答（以 respondent_name 前綴比對），不建立任何資料",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="先刪除整份示範問卷（含題目／關鍵字／所有填答）再重建",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="cleanup / reset 時略過互動確認（CI / 腳本使用）",
        )

    def handle(self, *args, **opts):
        if opts["cleanup"] and opts["reset"]:
            raise CommandError("--cleanup 與 --reset 不可同時使用")

        if opts["cleanup"]:
            self._cleanup_only(opts)
            return

        if opts["reset"]:
            self._reset(opts)

        if opts["count"] < 1:
            raise CommandError("--count 必須 >= 1")

        survey = self._ensure_survey()
        self._ensure_questions(survey)
        self._ensure_keywords(survey)

        rng = random.Random(opts["seed"])
        created = self._seed_responses(survey, opts["count"], rng)

        self.stdout.write(self.style.SUCCESS(
            f"完成：問卷 {survey.slug!r} 已建立／更新，灌入 {created} 筆模擬填答"
        ))
        self._print_db_hint()

    def _ensure_survey(self) -> Survey:
        category, _ = SurveyCategory.objects.get_or_create(name=CATEGORY_NAME)
        survey, _ = Survey.objects.update_or_create(
            slug=SURVEY_SLUG,
            defaults={
                "title": SURVEY_TITLE,
                "description": SURVEY_DESCRIPTION,
                "category": category,
                "thank_you_email_enabled": False,
                "improvement_tracking_enabled": True,
                "is_active": True,
            },
        )
        return survey

    def _ensure_questions(self, survey: Survey):
        for spec in QUESTIONS:
            Question.objects.update_or_create(
                survey=survey,
                order=spec["order"],
                defaults={k: v for k, v in spec.items() if k != "order"},
            )

    def _ensure_keywords(self, survey: Survey):
        for keyword, category in KEYWORDS:
            KeywordCategory.objects.update_or_create(
                survey=survey,
                keyword=keyword,
                defaults={"category": category, "threshold": 2},
            )

    def _seed_responses(self, survey: Survey, count: int, rng: random.Random) -> int:
        questions = list(survey.questions.order_by("order"))
        q_store = next(q for q in questions if q.order == 1)
        q_items = next(q for q in questions if q.order == 2)
        q_score = next(q for q in questions if q.order == 3)
        q_sweet = next(q for q in questions if q.order == 4)
        q_wait = next(q for q in questions if q.order == 5)
        q_text = next(q for q in questions if q.order == 6)

        per_store = count // len(STORES)
        remainder = count - per_store * len(STORES)
        store_cycle = [s for s in STORES for _ in range(per_store)]
        store_cycle.extend(rng.choices(STORES, k=remainder))
        rng.shuffle(store_cycle)

        with transaction.atomic():
            for i, store in enumerate(store_cycle, start=1):
                profile = STORE_PROFILES[store]
                lo, hi = profile["score_range"]
                slo, shi = profile["sweet_range"]
                items_lo, items_hi = profile["item_count"]
                items_pool = profile["item_pool"]

                wait = rng.choices(WAIT_LEVELS, weights=profile["wait_weights"])[0]
                k = rng.randint(items_lo, min(items_hi, len(items_pool)))
                items = rng.sample(items_pool, k=k)
                text = rng.choice(profile["text_pool"])

                answers = {
                    f"question_{q_store.id}": store,
                    f"question_{q_items.id}": items,
                    f"question_{q_score.id}": rng.randint(lo, hi),
                    f"question_{q_sweet.id}": rng.randint(slo, shi),
                    f"question_{q_wait.id}": wait,
                    f"question_{q_text.id}": text,
                }
                submit_survey_payload(
                    survey,
                    user=None,
                    respondent_name=f"{NAME_PREFIX} #{i}",
                    respondent_email="",
                    consent_follow_up=bool(rng.getrandbits(1)),
                    answers=answers,
                )
        return count

    def _cleanup_only(self, opts):
        survey = Survey.objects.filter(slug=SURVEY_SLUG).first()
        if not survey:
            self.stdout.write(self.style.WARNING(f"找不到問卷 {SURVEY_SLUG!r}，無需清理。"))
            self._print_db_hint()
            return
        qs = FeedbackSubmission.objects.filter(
            survey=survey, respondent_name__startswith=NAME_PREFIX,
        )
        total = qs.count()
        self.stdout.write(f"問卷 {SURVEY_SLUG!r} 中以前綴 {NAME_PREFIX!r} 命中的模擬填答數：{total}")
        self._print_db_hint()
        if total == 0:
            return
        if not opts["yes"]:
            confirm = input("輸入 yes 以繼續：").strip().lower()
            if confirm not in {"y", "yes"}:
                self.stdout.write(self.style.WARNING("已取消。"))
                return
        deleted, breakdown = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"已刪除 {deleted} 列（含關聯）"))
        for label, n in breakdown.items():
            self.stdout.write(f"  - {label}: {n}")

    def _reset(self, opts):
        existing = Survey.objects.filter(slug=SURVEY_SLUG).first()
        if not existing:
            return
        sub_count = existing.submissions.count()
        q_count = existing.questions.count()
        self.stdout.write(
            f"--reset：將刪除問卷 {SURVEY_SLUG!r}"
            f"（題目 {q_count}、所有填答 {sub_count} 筆，連同關聯資料）"
        )
        self._print_db_hint()
        if not opts["yes"]:
            confirm = input("輸入 yes 以繼續：").strip().lower()
            if confirm not in {"y", "yes"}:
                raise CommandError("已取消。")
        existing.delete()
        self.stdout.write(self.style.WARNING("已刪除既有示範問卷，準備重建。"))

    def _print_db_hint(self):
        s = connection.settings_dict
        self.stdout.write(
            f"目前使用資料庫：alias={connection.alias!r}, "
            f"ENGINE={s.get('ENGINE', '')!r}, NAME={s.get('NAME', '')!r}"
        )
