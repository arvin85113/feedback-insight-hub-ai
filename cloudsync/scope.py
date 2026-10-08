"""Which local surveys take part in result upload (spec §7, 2026-10-03 revision)."""

from cloudapi.models import SurveyDefinitionRevision
from feedback.analysis_sources import AnalysisSourceConfigurationError, resolve_analysis_source
from feedback.models import AnalysisJob


def is_cloud_synced(survey):
    """A synced definition with a valid Answer or immutable Parquet input binding.

    Small imports remain Answer inputs; external publication carries a separate
    immutable source identity instead of pretending to have an inbox watermark.
    """

    if not SurveyDefinitionRevision.objects.filter(survey=survey).exists():
        return False
    try:
        return resolve_analysis_source(survey).kind in AnalysisJob.SourceKind.values
    except AnalysisSourceConfigurationError:
        return False
