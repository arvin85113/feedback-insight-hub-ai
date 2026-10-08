"""Real HTTP between isolated node and cloud databases; tiny synthetic input only."""
import json
import tempfile
import uuid
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase, override_settings

from cloudsync.datasets import register_dataset
from cloudsync.models import CloudLink, ResultUpload
from cloudsync.runner import run_cycle
from cloudsync.testing import CloudServer
from cloudsync.tests.utils import memory_keyring
from cloudsync.tokens import save_token
from feedback.external_dataset import validate_external_dataset
from feedback.importing.mapping import load_mapping
from feedback.importing.service import mapping_definition
from feedback.models import AnalysisJob
from feedback.test_external_dataset import dataset_fixture


class ExternalEndToEndTests(TestCase):
    def test_register_worker_upload_and_cloud_read_latest_without_raw_rows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory, memory_keyring(), override_settings(CLOUD_SYNC_ALLOW_LOOPBACK_HTTP=True):
            root = Path(directory)
            with CloudServer(root) as cloud:
                link = CloudLink.relink(cloud.url, cloud.node_uuid)
                save_token(cloud.url, cloud.token)
                manifest, mapping, _ = dataset_fixture(root)
                mapping_data = json.loads(mapping.read_text(encoding="utf-8"))
                mapping_data.update(survey={"title": "Fixture reviews", "description": "Test only"},
                                    deduplication_fields=["source_identity_sha256"])
                mapping.write_text(json.dumps(mapping_data), encoding="utf-8")
                verified = validate_external_dataset(manifest, mapping)
                definition = mapping_definition(load_mapping(mapping), uuid.uuid4())
                survey, version = register_dataset(verified, definition, expected_version=0, generation=link.generation)
                # The exact HTTP replay does not create a second survey/source/job.
                repeated, _ = register_dataset(verified, definition, expected_version=0, generation=link.generation)
                self.assertEqual(repeated.pk, survey.pk)
                self.assertEqual(AnalysisJob.objects.filter(survey=survey, executor="deterministic", status="pending").count(), 1)
                call_command("run_analysis_worker", worker_id="e2e-external", output=str(root / "artifacts"), once=True)
                job = AnalysisJob.objects.filter(survey=survey, executor="deterministic").order_by("-pk").first()
                self.assertEqual(job.status, "succeeded", job.error_code)
                self.assertEqual(run_cycle(force=True), "ok")
                upload = ResultUpload.objects.get(survey=survey)
                self.assertEqual(upload.status, "uploaded")
                facts = json.loads(cloud.shell(f'''
import json
from feedback.models import Survey
from feedback.published_analysis import get_published_analysis_payload
s = Survey.objects.get(uuid="{survey.uuid}")
p = get_published_analysis_payload(s)
print(json.dumps({{"available": p["available"], "latest": p["node_result"]["is_latest"],
    "count": p["node_result"]["coverage"]["analyzed_unique"], "submissions": s.submissions.count(),
    "source": s.analysis_state.publication_manifest["input_source"]["source_version"]}}))
''').strip().splitlines()[-1])
                self.assertEqual(facts, {"available": True, "latest": True, "count": 4, "submissions": 0,
                    "source": version.source_version})
                self.assertNotIn(str(root), repr(upload.content))
