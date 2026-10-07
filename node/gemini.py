"""Credential-store access, version-bound consent and fail-closed per-call budgets."""

from datetime import timedelta
import hashlib
import json
import uuid

import keyring
from keyring.errors import PasswordDeleteError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from feedback.ai_stage_service import STAGE_MODULES, generate_stage, stage_prompt_version
from feedback.ai_worker import AIWorkerExecutionError, execute_ai_job
from feedback.analysis_jobs import check_claim_current, heartbeat_job, schedule_survey_analysis
from feedback.analysis_sources import external_version_identity, resolve_analysis_source
from feedback.models import AnalysisJob, Survey, SurveyAnalysisState
from .audit import record
from .models import NodeAIGrant
from organizations.access import organization_role
from organizations.models import OrganizationMembership

SERVICE = "FeedbackInsightHub"
KEY_NAME = "node-gemini-api-key"
GENERATION_NAME = "node-gemini-credential-generation"


def _vault():
    backend = keyring.get_keyring()
    if not getattr(settings, "NODE_TEST_CREDENTIAL_STORE", False):
        from keyring.backends.Windows import WinVaultKeyring
        if not isinstance(backend, WinVaultKeyring):
            raise AIWorkerExecutionError("windows_vault_required")
    return backend


def configured():
    return bool(_vault().get_password(SERVICE, KEY_NAME))


def set_key(value):
    vault = _vault()
    if value:
        vault.set_password(SERVICE, KEY_NAME, value)
    else:
        try:
            vault.delete_password(SERVICE, KEY_NAME)
        except PasswordDeleteError:
            pass
    vault.set_password(SERVICE, GENERATION_NAME, str(uuid.uuid4()))


def current_identity(survey_id):
    state = SurveyAnalysisState.objects.select_related("published_snapshot").get(survey_id=survey_id)
    snapshot = state.published_snapshot
    if not snapshot:
        raise AIWorkerExecutionError("base_snapshot_missing")
    for name in ("statistics", "text"):
        item = (state.publication_manifest or {}).get(name) or {}
        if (item.get("snapshot_id"), item.get("input_version"), item.get("config_version"), item.get("pipeline_version")) != (
            snapshot.pk, state.input_version, state.config_version, state.pipeline_version
        ):
            raise AIWorkerExecutionError("base_snapshot_not_current")
    survey = Survey.objects.get(pk=survey_id)
    binding = resolve_analysis_source(survey)
    source = {"kind": "answers"}
    if binding.is_external:
        source = external_version_identity(survey.analysis_source.active_external_version)
        item = (state.publication_manifest or {}).get("statistics") or {}
        if (item.get("source_ref"), item.get("source_version")) != (binding.source_ref, binding.source_version):
            raise AIWorkerExecutionError("base_source_not_current")
    return {
        "survey": survey_id, "snapshot": snapshot.pk, "fingerprint": snapshot.data_fingerprint,
        "input": state.input_version, "config": state.config_version, "pipeline": state.pipeline_version,
        "definition": survey.definition_version, "source": source, "model": settings.GEMINI_MODEL,
        "prompts": {name: stage_prompt_version(module) for name, module in STAGE_MODULES.items()},
        "credential_generation": _vault().get_password(SERVICE, GENERATION_NAME) or "",
    }


def confirm(survey_id, actor, identity, *, confirmation_uuid=None):
    if not actor.is_active or organization_role(actor) != OrganizationMembership.Role.OWNER:
        raise AIWorkerExecutionError("owner_required")
    # Lock the same domain rows used by mutations before comparing the signed preview.
    with transaction.atomic():
        Survey.objects.select_for_update().get(pk=survey_id)
        SurveyAnalysisState.objects.select_for_update().get(survey_id=survey_id)
        if not configured() or current_identity(survey_id) != identity:
            raise AIWorkerExecutionError("confirmation_expired")
        confirmation_uuid = confirmation_uuid or uuid.uuid5(uuid.NAMESPACE_URL,
            f"{actor.pk}:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
        existing = NodeAIGrant.objects.filter(confirmation_uuid=confirmation_uuid).first()
        if existing:
            if existing.actor_id != actor.pk or existing.identity != identity:
                raise AIWorkerExecutionError("confirmation_expired")
            # The same signed confirmation never creates another paid run, including failures.
            return existing
        if NodeAIGrant.objects.filter(job__survey_id=survey_id, in_flight=True).exists():
            raise AIWorkerExecutionError("previous_call_uncertain")
        active = NodeAIGrant.objects.filter(job__survey_id=survey_id, status__in=("queued", "running")).first()
        if active:
            if active.identity == identity and active.expires_at > timezone.now():
                raise AIWorkerExecutionError("paid_job_already_pending")
            # Obsolete consent does not block a new version forever. No in-flight calls here.
            from feedback.analysis_jobs import request_job_cancel
            request_job_cancel(active.job_id)
            NodeAIGrant.objects.filter(pk=active.pk).update(status="stale", error_code="confirmation_expired")
        job = schedule_survey_analysis(survey_id, change="none", requested_stages=("synthesis",))
        grant = NodeAIGrant.objects.create(job=job, actor=actor, identity=identity, confirmation_uuid=confirmation_uuid,
            expires_at=timezone.now() + timedelta(hours=1))
        record("ai.confirmed", actor=actor, target=survey_id, job_id=job.pk,
               snapshot_id=identity["snapshot"], model=identity["model"], max_calls=grant.max_calls)
        return grant


class AuthorizedClient:
    def __init__(self, client, job, lease_seconds):
        self.client, self.job, self.lease_seconds = client, job, lease_seconds
        self.models = self

    def generate_content(self, **kwargs):
        job = self.job
        beat = heartbeat_job(job.pk, job.lease_token, lease_seconds=self.lease_seconds)
        if not beat.accepted or not check_claim_current(job.pk, job.lease_token):
            raise AIWorkerExecutionError("lease_or_version_changed")
        with transaction.atomic():
            Survey.objects.select_for_update().get(pk=job.survey_id)
            SurveyAnalysisState.objects.select_for_update().get(survey_id=job.survey_id)
            grant = NodeAIGrant.objects.select_for_update().select_related("actor").get(job=job)
            if (grant.status not in ("queued", "running") or grant.in_flight
                    or grant.expires_at <= timezone.now() or grant.identity != current_identity(job.survey_id)
                    or not grant.actor.is_active or organization_role(grant.actor) != OrganizationMembership.Role.OWNER
                    or kwargs.get("model") != grant.identity["model"] or grant.calls_started >= grant.max_calls):
                raise AIWorkerExecutionError("paid_grant_invalid")
            grant.calls_started += 1
            grant.in_flight, grant.status = True, "running"
            grant.save(update_fields=["calls_started", "in_flight", "status"])
        # The reservation commits BEFORE contacting the provider. Crash/timeout remains uncertain.
        try:
            response = self.client.models.generate_content(**kwargs)
        except Exception:
            NodeAIGrant.objects.filter(job=job).update(status="uncertain", error_code="provider_result_uncertain")
            raise
        NodeAIGrant.objects.filter(job=job).update(in_flight=False)
        return response

    def close(self):
        self.client.close()


def execute_confirmed(job, *, lease_seconds=600):
    grant = NodeAIGrant.objects.get(job=job)

    def factory():
        from google import genai
        key = _vault().get_password(SERVICE, KEY_NAME)
        if not key:
            raise AIWorkerExecutionError("key_missing")
        return AuthorizedClient(genai.Client(vertexai=True, api_key=key), job, lease_seconds)

    try:
        if (grant.status not in ("queued", "running") or grant.in_flight or grant.expires_at <= timezone.now()
                or grant.identity != current_identity(job.survey_id)):
            raise AIWorkerExecutionError("paid_grant_invalid")
        result = execute_ai_job(job, allow_paid_ai=True, lease_seconds=lease_seconds,
            stage_runner=lambda snapshot, stage: generate_stage(snapshot, stage, client_factory=factory))
    except Exception:
        NodeAIGrant.objects.filter(job=job, in_flight=False).update(status="failed", error_code="ai_run_failed")
        raise
    NodeAIGrant.objects.filter(job=job).update(status="completed", error_code="")
    record("ai.completed", actor=grant.actor, target=job.survey_id, job_id=job.pk, snapshot_id=result.snapshot_id)
    return result
