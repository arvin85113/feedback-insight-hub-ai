"""Read-only adapters. ORM acquisition belongs in a worker, never page GET."""
import json
from pathlib import Path

from .analysis_input import AnalysisField
from .importing.local_dataset import file_sha256


class TableInput:
    source_kind = "external_dataset"

    def __init__(self, rows, fields, *, version, name):
        self.rows = rows
        self._fields = tuple(fields)
        self.dataset_version = version
        self.name = name

    def fields(self):
        return self._fields

    def scan(self, columns):
        for row in self.rows:
            yield {name: row.get(name) for name in columns}


class AnswerInput(TableInput):
    source_kind = "native_answers"

    def __init__(
        self,
        rows,
        fields,
        *,
        version,
        name,
        survey_id=None,
        database_alias="default",
    ):
        super().__init__(rows, fields, version=version, name=name)
        self.survey_id = survey_id
        self.database_alias = database_alias

    @classmethod
    def from_answers(cls, answers, question_fields, *, version, name, submission_ids=()):
        # Materialize a fixed worker input. No names, emails, respondent IDs or ORM writes.
        rows = {pk: {} for pk in submission_ids}
        for answer in answers:
            field = question_fields[answer.question_id]
            rows.setdefault(answer.submission_id, {})[field.name] = answer.value
        return cls(list(rows.values()), question_fields.values(), version=version, name=name)

    @classmethod
    def from_survey(cls, survey, *, version):
        alias = survey._state.db or "default"
        fields = {
            q.pk: AnalysisField(
                f"question_{q.pk}",
                q.data_type,
                True,
                q.title,
                q.kind,
                tuple(q.options),
                q.enable_keyword_tracking,
            )
            for q in survey.questions.filter(is_active=True).order_by("order", "id")
        }
        adapter = cls(
            None,
            fields.values(),
            version=version,
            name=survey.title,
            survey_id=survey.pk,
            database_alias=alias,
        )
        adapter.question_fields = fields
        adapter.keyword_rules = list(
            survey.keyword_categories.values("keyword", "category", "threshold")
        )
        return adapter

    def scan(self, columns):
        if self.survey_id is None:
            yield from super().scan(columns)
            return

        from django.db.models import OuterRef, Subquery

        from .models import Answer, FeedbackSubmission

        known = {field.name for field in self.fields()}
        if not set(columns) <= known:
            raise ValueError("column not in analysis mapping")
        if not columns:
            submission_ids = (
                FeedbackSubmission.objects.using(self.database_alias)
                .filter(
                    survey_id=self.survey_id,
                    is_complete=True,
                    voided_at__isnull=True,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            for _submission_id in submission_ids.iterator(chunk_size=2000):
                yield {}
            return
        question_ids = {field.name: question_id for question_id, field in self.question_fields.items()}
        annotations = {}
        value_query = Answer.objects.using(self.database_alias).filter(
            submission_id=OuterRef("pk")
        )
        for index, name in enumerate(columns):
            annotations[f"analysis_value_{index}"] = Subquery(
                value_query.filter(question_id=question_ids[name]).values("value")[:1]
            )
        value_names = tuple(annotations)
        queryset = (
            FeedbackSubmission.objects.using(self.database_alias)
            .filter(
                survey_id=self.survey_id,
                is_complete=True,
                voided_at__isnull=True,
            )
            .order_by("pk")
            .annotate(**annotations)
            .values_list(*value_names)
        )
        for values in queryset.iterator(chunk_size=2000):
            yield dict(zip(columns, values))


class ParquetInput(TableInput):
    def __init__(self, manifest_path, mapping_path):
        manifest_path = Path(manifest_path).resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = manifest_path.parent.parent
        self.path = (root / manifest["clean_path"]).resolve()
        if not self.path.is_relative_to(root):
            raise ValueError("clean_path escapes dataset root")
        artifact = next(a for a in manifest["artifacts"] if a["path"] == manifest["clean_path"])
        if self.path.stat().st_size != artifact["size"] or file_sha256(self.path) != artifact["sha256"]:
            raise ValueError("clean integrity mismatch")
        self.file_signature = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        mapping = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
        source = manifest["source"]
        if mapping["dataset"]["name"] != source["dataset"] or mapping["dataset"]["version"] != source["source_revision"]:
            raise ValueError("mapping/source version mismatch")
        fields = []
        for question in mapping["questions"]:
            field_name = ("review_length" if question.get("normalizers") == ["text_length"]
                          else question["source_field"])
            fields.append(AnalysisField(field_name, question["data_type"], not question["required"],
                question["title"], question["kind"], tuple(question.get("options", ())),
                question.get("enable_keyword_tracking", False)))
        version = ":".join((source["source_revision"], manifest["cleaning_version"], artifact["sha256"]))
        super().__init__(None, fields, version=version, name=source["dataset"])

    def verify_unchanged(self):
        if (self.path.stat().st_size, self.path.stat().st_mtime_ns) != self.file_signature:
            raise ValueError("input changed during analysis; no result published")

    def scan(self, columns):
        import duckdb
        allowed = {field.name for field in self.fields()}
        if not set(columns) <= allowed:
            raise ValueError("column not in analysis mapping")
        with duckdb.connect(":memory:") as connection:
            connection.execute("SET threads=2")
            names = ', '.join('"' + name.replace('"', '""') + '"' for name in columns)
            cursor = connection.execute(f"SELECT {names} FROM read_parquet(?)", [str(self.path)])
            while batch := cursor.fetchmany(2048):
                for values in batch:
                    yield dict(zip(columns, values))
