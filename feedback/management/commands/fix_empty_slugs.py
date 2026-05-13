from django.core.management.base import BaseCommand
from django.utils.text import slugify

from feedback.models import Survey


class Command(BaseCommand):
    help = "Fix surveys with empty or malformed slugs (e.g. Chinese-only titles)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        bad = list(Survey.objects.filter(slug="")) + list(
            Survey.objects.filter(slug__startswith="-")
        )
        if not bad:
            self.stdout.write("No bad slugs found.")
            return
        for survey in bad:
            base = slugify(survey.title) or f"survey-{survey.pk}"
            slug = base
            counter = 2
            while Survey.objects.filter(slug=slug).exclude(pk=survey.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.stdout.write(f"  id={survey.pk}: {repr(survey.slug)} -> {repr(slug)}")
            if not dry_run:
                survey.slug = slug
                survey.save(update_fields=["slug"])
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes saved."))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
