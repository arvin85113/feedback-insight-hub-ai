import random
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from feedback.local_service import submit_survey_payload
from feedback.models import FeedbackSubmission, Survey
from feedback.seed_support import (
    answers_by_title,
    choice_question,
    create_published_node_survey,
    create_published_survey,
    number_question,
    scale_question,
    text_question,
)
from feedback.survey_purge import PurgeRefused, purge_survey

SURVEY_SLUG = "beverage-feedback"
SURVEY_TITLE = "飲料店體驗回饋"
SURVEY_DESCRIPTION = "蒐集顧客在不同門市的飲料體驗，作為服務優化與品項調整參考。"
CATEGORY_NAME = "飲料店"
NAME_PREFIX = "飲料店模擬填答"
# Fixed so the cloud's self-test inbox allowlist can name it before the node creates it (spec §1, §4).
BEVERAGE_NODE_SURVEY_UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://feedbackhub.local/demo/beverage-node")

STORES = ["信義店", "台北車站店", "公館店", "士林店"]
DINING = ["內用", "外帶"]
ITEMS = ["珍珠奶茶", "紅茶", "綠茶", "水果茶", "咖啡", "奶蓋茶"]
WAIT_LEVELS = ["很快", "普通", "久", "很久"]
NOT_APPLICABLE = "不適用"

# Builder spec §5 step 6: every question type and all seven statistical methods are exercised.
QUESTIONS = [
    choice_question("門市", STORES),
    choice_question("內用或外帶", DINING),
    choice_question("最常購買的品項（可複選）", ITEMS, multiple=True),
    scale_question("整體滿意度", 1, 10, low_label="非常不滿意", high_label="非常滿意"),
    scale_question("推薦意願", 0, 10, low_label="完全不會", high_label="一定會"),
    choice_question("等候時間感受", WAIT_LEVELS + [NOT_APPLICABLE], ordered=True, excluded=(NOT_APPLICABLE,)),
    number_question("等候分鐘數", decimal=True),
    number_question("消費金額", decimal=True),
    number_question("過去 30 天來店次數"),
    text_question("希望改善的地方", long=True, tracked=True, required=False),
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
        "wait_mean": 3.0,
        "visits": (3, 8),
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
        "wait_mean": 9.0,
        "visits": (1, 4),
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
        "wait_mean": 5.0,
        "visits": (2, 6),
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
        "wait_mean": 8.0,
        "visits": (1, 3),
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


def simulated_responses(rng: random.Random, count: int) -> list[tuple[int, dict, bool]]:
    """(number, {question title: value}, consent_follow_up) for each simulated reply; one rng order for every path."""

    per_store = count // len(STORES)
    store_cycle = [store for store in STORES for _ in range(per_store)]
    store_cycle.extend(rng.choices(STORES, k=count - len(store_cycle)))
    rng.shuffle(store_cycle)
    responses = []
    for i, store in enumerate(store_cycle, start=1):
        profile = STORE_PROFILES[store]
        dining = rng.choice(DINING)
        lo, hi = profile["score_range"]
        # Take-away customers wait less and are a little happier (Welch t / Mann-Whitney).
        wait_minutes = round(max(0.5, rng.gauss(profile["wait_mean"], 1.5) - (1.5 if dining == "外帶" else 0)), 1)
        score = min(10, max(1, rng.randint(lo, hi) + (1 if dining == "外帶" else 0) - int(wait_minutes // 6)))
        spend = round(max(35.0, rng.gauss(55 + score * 6, 12)), 1)  # higher spend with higher satisfaction
        wait_feeling = WAIT_LEVELS[min(3, int(wait_minutes // 3.5))]
        if rng.random() < 0.05:
            wait_feeling = NOT_APPLICABLE  # e.g. delivered by a friend: excluded from analysis
        items = rng.sample(profile["item_pool"], k=rng.randint(1, min(profile["item_count"][1],
                                                                 len(profile["item_pool"]))))
        values = {
            "門市": store,
            "內用或外帶": dining,
            "最常購買的品項（可複選）": items,
            "整體滿意度": str(score),
            "推薦意願": str(min(10, max(0, score + rng.randint(-2, 1)))),
            "等候時間感受": wait_feeling,
            "等候分鐘數": f"{wait_minutes}",
            "消費金額": f"{spend}",
            "過去 30 天來店次數": str(rng.randint(profile["visits"][0], profile["visits"][1])),
            "希望改善的地方": rng.choice(profile["text_pool"]),
        }
        responses.append((i, values, bool(rng.getrandbits(1))))
    return responses


class Command(BaseCommand):
    help = (
        "建立並發布飲料店示範問卷（10 題、6 條關鍵字分類），灌入模擬填答（填答者名稱前綴「飲料店模擬填答」）。"
        "問卷已存在時需加 --reset 重建；--cleanup 只清除模擬填答。"
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
            "--node-create",
            action="store_true",
            help="本機節點模式：經雲端 API 建立並發布節點擁有的飲料店問卷（固定 UUID）與關鍵字，不灌填答",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="cleanup / reset 時略過互動確認（CI / 腳本使用）",
        )

    def handle(self, *args, **opts):
        if opts["cleanup"] and opts["reset"]:
            raise CommandError("--cleanup 與 --reset 不可同時使用")

        if opts["node_create"]:
            self._node_create(opts)
            return

        if opts["cleanup"]:
            self._cleanup_only(opts)
            return

        if opts["reset"]:
            self._reset(opts)

        if opts["count"] < 1:
            raise CommandError("--count 必須 >= 1")

        survey = create_published_survey(
            slug=SURVEY_SLUG, title=SURVEY_TITLE, description=SURVEY_DESCRIPTION, category=CATEGORY_NAME,
            questions=QUESTIONS, keywords=KEYWORDS,
        )
        rng = random.Random(opts["seed"])
        created = self._seed_responses(survey, opts["count"], rng)

        self.stdout.write(self.style.SUCCESS(
            f"完成：問卷 {survey.slug!r} 已建立並發布，灌入 {created} 筆模擬填答"
        ))
        self._print_db_hint()

    def _seed_responses(self, survey: Survey, count: int, rng: random.Random) -> int:
        with transaction.atomic():
            for i, values, consent in simulated_responses(rng, count):
                submit_survey_payload(
                    survey,
                    user=None,
                    respondent_name=f"{NAME_PREFIX} #{i}",
                    respondent_email="",
                    consent_follow_up=consent,
                    answers=answers_by_title(survey, values),
                )
        return count

    def _node_create(self, opts):
        if not settings.IS_NODE:
            raise CommandError("--node-create 只能在本機節點模式執行")
        if opts["reset"] or opts["cleanup"]:
            raise CommandError("--node-create 不可與 --reset、--cleanup 併用")
        survey = create_published_node_survey(
            survey_uuid=BEVERAGE_NODE_SURVEY_UUID, title=SURVEY_TITLE, description=SURVEY_DESCRIPTION,
            category=CATEGORY_NAME, questions=QUESTIONS, keywords=KEYWORDS,
        )
        self.stdout.write(self.style.SUCCESS(
            f"完成：節點問卷已建立並發布（UUID {survey.uuid}，slug {survey.slug}）；"
            "模擬填答請在雲端執行 --inbox"
        ))

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
        try:
            purge_survey(existing)
        except PurgeRefused as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.WARNING("已刪除既有示範問卷，準備重建。"))

    def _print_db_hint(self):
        s = connection.settings_dict
        self.stdout.write(
            f"目前使用資料庫：alias={connection.alias!r}, "
            f"ENGINE={s.get('ENGINE', '')!r}, NAME={s.get('NAME', '')!r}"
        )
