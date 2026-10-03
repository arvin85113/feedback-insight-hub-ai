import re
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles import finders

# Features that exist only on the public cloud site (landing page, customer
# sign-up, the shared login entry).  The local node CI job skips them.
cloud_only = skipUnless(settings.DEPLOYMENT_MODE == "cloud", "雲端模式專用功能")


def page_with_scripts(response):
    """Rendered HTML followed by the source of every local static script it loads.

    Page behaviour lives in static/js files, so assertions about what a page can
    do must look at both the markup and the scripts it references.
    """

    html = response.content.decode()
    static_prefix = re.escape(settings.STATIC_URL.lstrip("/"))
    sources = [html]
    for path in re.findall(rf'<script src="/?{static_prefix}([^"?]+)', html):
        found = finders.find(path)
        assert found, f"page references missing static script {path}"
        with open(found, encoding="utf-8") as handle:
            sources.append(handle.read())
    return "\n".join(sources)


def published(survey):
    """Mark a test survey as published (spec §4) without going through the builder; returns it refreshed."""

    from feedback.models import Survey

    version = survey.definition_version or 1
    Survey.objects.filter(pk=survey.pk).update(
        definition_version=version, published_version=version, analysis_definition_version=version,
    )
    survey.refresh_from_db()
    return survey
