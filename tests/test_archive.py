import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.archive_run import export_trace


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run_test.json"
        self.trace = {"goal": "Study databases", "steps": [], "final_output": "Notes"}
        self.path.write_text(json.dumps(self.trace))

    def test_dry_run_does_not_create_any_cloud_client(self):
        with patch("boto3.client") as aws, patch("azure.identity.DefaultAzureCredential") as azure, \
             patch("firebase_admin.initialize_app") as firebase:
            plans = [export_trace(self.path, provider, "test", "https://example.blob.core.windows.net", True)
                     for provider in ("aws", "azure", "firebase")]
            self.assertTrue(all(p["dry_run"] for p in plans))
            aws.assert_not_called(); azure.assert_not_called(); firebase.assert_not_called()

    def test_s3_upload_preserves_trace_and_content_type(self):
        with patch("boto3.client") as client:
            plan = export_trace(self.path, "aws", "private-test-bucket")
            call = client.return_value.put_object.call_args.kwargs
            self.assertEqual(call["Bucket"], "private-test-bucket")
            self.assertEqual(call["Key"], plan["key"])
            self.assertEqual(json.loads(call["Body"]), self.trace)
            self.assertEqual(call["ContentType"], "application/json")
            self.assertNotIn("ACL", call)

    def test_azure_uses_identity_and_correct_container(self):
        with patch("azure.identity.DefaultAzureCredential") as identity, \
             patch("azure.storage.blob.BlobServiceClient") as service:
            plan = export_trace(self.path, "azure", "research", "https://test.blob.core.windows.net")
            service.assert_called_once_with(account_url="https://test.blob.core.windows.net",
                credential=identity.return_value.__enter__.return_value)
            active = service.return_value.__enter__.return_value
            active.get_blob_client.assert_called_once_with(container="research", blob=plan["key"])
            upload = active.get_blob_client.return_value.upload_blob
            self.assertEqual(json.loads(upload.call_args.args[0]), self.trace)

    def test_firestore_stores_summary_not_raw_trace(self):
        with patch("firebase_admin.get_app", return_value=Mock()) as get_app, \
             patch("firebase_admin.firestore.client") as client:
            plan = export_trace(self.path, "firebase", "research_runs")
            client.assert_called_once_with(app=get_app.return_value)
            document = client.return_value.collection.return_value.document
            document.assert_called_once_with(plan["key"])
            saved = document.return_value.set.call_args.args[0]
            self.assertEqual(saved["goal"], self.trace["goal"])
            self.assertNotIn("steps", saved)

    def test_same_content_uses_same_key(self):
        one = export_trace(self.path, "aws", "test", dry_run=True)
        other = self.path.with_name("renamed.json")
        other.write_bytes(self.path.read_bytes())
        self.assertEqual(one["key"], export_trace(other, "aws", "test", dry_run=True)["key"])

    def test_invalid_trace_rejected_before_upload(self):
        self.path.write_text("[]")
        with patch("boto3.client") as client, self.assertRaises(ValueError):
            export_trace(self.path, "aws", "test")
        client.assert_not_called()
