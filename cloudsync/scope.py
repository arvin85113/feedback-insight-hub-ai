"""Which local surveys take part in result upload (spec §7, 2026-10-03 revision)."""

from cloudapi.models import SurveyDefinitionRevision
from feedback.analysis_sources import AnalysisSourceConfigurationError, resolve_analysis_source
from feedback.models import AnalysisJob


def is_cloud_synced(survey):
    """Synced from the cloud and analysed from its own replies.

    External-dataset surveys are excluded: their imports commit in chunks and bump
    versions only at the end, which breaks the version-invalidation premise.
    """

    if not SurveyDefinitionRevision.objects.filter(survey=survey).exists():
        return False
    try:
        return resolve_analysis_source(survey).kind == AnalysisJob.SourceKind.ANSWERS
    except AnalysisSourceConfigurationError:
        return False
