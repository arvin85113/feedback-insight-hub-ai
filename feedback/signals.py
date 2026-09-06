"""Persist analysis invalidations when survey inputs or rules change."""

from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from .analysis_jobs import schedule_survey_analysis, scheduling_is_suppressed
from .models import Answer, FeedbackSubmission, KeywordCategory, Question, Survey


def _schedule(survey_id, change):
    if survey_id and not scheduling_is_suppressed():
        schedule_survey_analysis(survey_id, change=change)


@receiver(post_save, sender=FeedbackSubmission, dispatch_uid="analysis_submission_saved")
def submission_saved(sender, instance, **kwargs):
    _schedule(instance.survey_id, "input")


@receiver(post_delete, sender=FeedbackSubmission, dispatch_uid="analysis_submission_deleted")
def submission_deleted(sender, instance, **kwargs):
    _schedule(instance.survey_id, "input")


@receiver(post_save, sender=Answer, dispatch_uid="analysis_answer_saved")
def answer_saved(sender, instance, update_fields=None, **kwargs):
    derived_fields = {"analysis_text", "sentiment_score", "analysis_version"}
    if update_fields is not None and set(update_fields) <= derived_fields:
        return
    survey_id = (
        FeedbackSubmission.objects.filter(pk=instance.submission_id)
        .values_list("survey_id", flat=True)
        .first()
    )
    _schedule(survey_id, "input")


@receiver(pre_delete, sender=Answer, dispatch_uid="analysis_answer_cache_survey")
def answer_cache_survey(sender, instance, **kwargs):
    instance._analysis_survey_id = (
        FeedbackSubmission.objects.filter(pk=instance.submission_id)
        .values_list("survey_id", flat=True)
        .first()
    )


@receiver(post_delete, sender=Answer, dispatch_uid="analysis_answer_deleted")
def answer_deleted(sender, instance, **kwargs):
    _schedule(getattr(instance, "_analysis_survey_id", None), "input")


@receiver(post_save, sender=Question, dispatch_uid="analysis_question_saved")
@receiver(post_delete, sender=Question, dispatch_uid="analysis_question_deleted")
def question_changed(sender, instance, **kwargs):
    _schedule(instance.survey_id, "config")


@receiver(post_save, sender=Survey, dispatch_uid="analysis_survey_saved")
def survey_changed(sender, instance, created=False, update_fields=None, **kwargs):
    if created:
        return
    relevant = {
        "title",
        "description",
        "analysis_enabled",
        "archived_at",
    }
    if update_fields is None or set(update_fields) & relevant:
        _schedule(instance.pk, "config")


@receiver(post_save, sender=KeywordCategory, dispatch_uid="analysis_keyword_rule_saved")
@receiver(post_delete, sender=KeywordCategory, dispatch_uid="analysis_keyword_rule_deleted")
def keyword_rule_changed(sender, instance, **kwargs):
    _schedule(instance.survey_id, "config")
