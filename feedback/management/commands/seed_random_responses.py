import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from feedback.local_service import submit_survey_payload
from feedback.models import FeedbackSubmission, Question, Survey

User = get_user_model()

# 短句與長句片段：利於關鍵字／情緒管線產生較有變化的文字題資料
_TEXT_SHORT = [
    "載入偏慢",
    "介面清楚好用",
    "希望通知更即時",
    "統計圖表很實用",
    "操作流程順暢",
    "偶爾會卡住",
    "整體滿意",
    "建議優化速度",
]

_TEXT_LONG_PARTS = [
    "整體體驗不錯，介面直覺、通知功能實用。",
    "統計圖表能幫助我們掌握趨勢，但匯出流程可以再簡化。",
    "系統回應速度有時偏慢，尤其在尖峰時段。",
    "希望後續能加強行動裝置上的版面適配。",
    "客服回覆即時，改善追蹤讓人感到被重視。",
    "部分頁面資訊密度偏高，建議分區呈現。",
    "資料正確性良好，搜尋條件若能儲存常用組合會更好。",
]


def _random_answer_for_question(question: Question, rng: random.Random):
    """產生與 SurveyFormBuilder / submit_survey_payload 相容的值（多選為 list）。"""
    opts = [line.strip() for line in question.options_text.splitlines() if line.strip()]

    if question.kind == Question.Kind.SHORT_TEXT:
        return rng.choice(_TEXT_SHORT)
    if question.kind == Question.Kind.LONG_TEXT:
        n = rng.randint(2, min(4, len(_TEXT_LONG_PARTS)))
        parts = rng.sample(_TEXT_LONG_PARTS, k=n)
        return " ".join(parts)
    if question.kind == Question.Kind.SINGLE_CHOICE:
        if not opts:
            return "（無選項）"
        return rng.choice(opts)
    if question.kind == Question.Kind.MULTIPLE_CHOICE:
        if not opts:
            return []
        k = rng.randint(1, len(opts))
        return rng.sample(opts, k=k)
    if question.kind == Question.Kind.INTEGER:
        return rng.randint(1, 100)
    if question.kind == Question.Kind.DECIMAL:
        return f"{rng.uniform(0, 999.99):.2f}"
    if question.kind == Question.Kind.SCALE:
        if opts:
            return rng.choice(opts)
        return rng.randint(1, 5)
    return ""


def _build_answers_dict(survey: Survey, rng: random.Random) -> dict:
    out = {}
    for q in survey.questions.all():
        out[f"question_{q.id}"] = _random_answer_for_question(q, rng)
    return out


class Command(BaseCommand):
    help = (
        "依指定問卷隨機建立多筆填答（寫入 Django 設定的預設資料庫；"
        "若 DATABASE_URL 指向 Supabase，即會寫入該雲端資料庫）。"
    )

    def add_arguments(self, parser):
        parser.add_argument("survey", type=str, help="問卷 slug（例如 product-feedback）")
        parser.add_argument(
            "--count",
            type=int,
            default=20,
            help="要建立幾筆 FeedbackSubmission（預設 20）",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="隨機種子，便於重現同一組假資料",
        )
        parser.add_argument(
            "--spread-days",
            type=float,
            default=0,
            metavar="DAYS",
            help="將 submitted_at 隨機落在「現在往前 0～DAYS 天」區間（0 表示全部為建立當下）",
        )
        parser.add_argument(
            "--as-user",
            type=str,
            default="",
            metavar="USERNAME",
            help="若指定，所有新建立的填答都掛在此既有使用者底下（須已存在）",
        )
        parser.add_argument(
            "--name-prefix",
            type=str,
            default="隨機測試填答",
            help="respondent_name 前綴（預設：隨機測試填答）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只預覽第一筆將產生的答案摘要，不寫入資料庫",
        )
        parser.add_argument(
            "--cleanup",
            action="store_true",
            help=(
                "刪除先前由本指令建立的測試填答（以 respondent_name 前綴比對；"
                "預設前綴 = --name-prefix）。不會刪除問卷、題目、KeywordCategory、"
                "ImprovementUpdate 或任何使用者帳號。"
            ),
        )
        parser.add_argument(
            "--cleanup-prefix",
            type=str,
            default="",
            metavar="PREFIX",
            help="覆寫 cleanup 比對前綴（不指定則使用 --name-prefix）",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="cleanup 時略過互動確認（CI / 腳本使用）",
        )

    def handle(self, *args, **options):
        slug = options["survey"].strip()

        survey = Survey.objects.filter(slug=slug).first()
        if not survey:
            raise CommandError(f"找不到 slug 為 {slug!r} 的問卷")

        if options["cleanup"]:
            self._handle_cleanup(survey, options)
            return

        count = options["count"]
        if count < 1:
            raise CommandError("--count 必須 >= 1")

        if not survey.questions.exists():
            raise CommandError(f"問卷 {slug!r} 沒有任何題目，無法產生填答")

        user = None
        if options["as_user"]:
            user = User.objects.filter(username=options["as_user"]).first()
            if not user:
                raise CommandError(f"找不到使用者：{options['as_user']!r}")

        rng = random.Random(options["seed"])

        if options["dry_run"]:
            sample = _build_answers_dict(survey, rng)
            self.stdout.write(self.style.WARNING("DRY-RUN：不會寫入資料庫"))
            self.stdout.write(f"問卷：{survey.title} ({survey.slug})，預計筆數：{count}")
            for k, v in list(sample.items())[:8]:
                self.stdout.write(f"  {k}: {v!r}")
            if len(sample) > 8:
                self.stdout.write(f"  … 其餘 {len(sample) - 8} 題省略")
            self._print_db_hint()
            return

        spread = max(0.0, float(options["spread_days"]))
        prefix = options["name_prefix"].strip() or "隨機測試填答"

        created_ids = []
        for i in range(count):
            answers = _build_answers_dict(survey, rng)
            payload = submit_survey_payload(
                survey,
                user=user,
                respondent_name=f"{prefix} #{i + 1}",
                respondent_email="",
                consent_follow_up=bool(rng.getrandbits(1)),
                answers=answers,
            )
            sid = payload["submission_id"]
            created_ids.append(sid)
            if spread > 0:
                delta = timedelta(seconds=rng.uniform(0, spread * 86400))
                ts = timezone.now() - delta
                FeedbackSubmission.objects.filter(pk=sid).update(submitted_at=ts)

        self.stdout.write(
            self.style.SUCCESS(f"已建立 {len(created_ids)} 筆填答（問卷 {survey.slug}）")
        )
        self._print_db_hint()

    def _handle_cleanup(self, survey: Survey, options):
        prefix = (options["cleanup_prefix"] or options["name_prefix"]).strip()
        if not prefix:
            raise CommandError("cleanup 比對前綴不可為空")

        qs = FeedbackSubmission.objects.filter(
            survey=survey, respondent_name__startswith=prefix
        )
        total = qs.count()
        self.stdout.write(
            f"問卷 {survey.slug!r} 中以 respondent_name 前綴 {prefix!r} 命中的填答數：{total}"
        )
        self._print_db_hint()

        if total == 0:
            self.stdout.write(self.style.WARNING("沒有可清除的紀錄。"))
            return

        if options["dry_run"]:
            sample = list(qs.values_list("id", "respondent_name", "submitted_at")[:5])
            self.stdout.write(self.style.WARNING("DRY-RUN：不會刪除任何資料。前 5 筆預覽："))
            for sid, name, submitted_at in sample:
                self.stdout.write(f"  id={sid} name={name!r} submitted_at={submitted_at}")
            return

        if not options["yes"]:
            confirm = input(
                f"將刪除 {total} 筆 FeedbackSubmission（連同其 Answer 與 ImprovementDispatch），"
                f"輸入 yes 以繼續：" 
            ).strip().lower()
            if confirm not in {"y", "yes"}:
                self.stdout.write(self.style.WARNING("已取消。"))
                return

        deleted, breakdown = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"已刪除 {deleted} 列（含關聯）"))
        for model_label, n in breakdown.items():
            self.stdout.write(f"  - {model_label}: {n}")

    def _print_db_hint(self):
        alias = connection.alias
        settings_dict = connection.settings_dict
        engine = settings_dict.get("ENGINE", "")
        name = settings_dict.get("NAME", "")
        self.stdout.write(
            f"目前使用資料庫：alias={alias!r}, ENGINE={engine!r}, NAME={name!r}"
        )
