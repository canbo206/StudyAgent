"""Explicit, optional cloud export. Dry-run validates without credentials/network."""

import argparse
import hashlib
import json
from pathlib import Path

try:
    from .trace_store import read_trace, trace_summary
except ImportError:
    from trace_store import read_trace, trace_summary


def export_trace(path, provider, destination, account_url=None, dry_run=False):
    data = read_trace(path)
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    # Content addressing makes retries idempotent without overwriting other runs.
    digest = hashlib.sha256(payload).hexdigest()
    key = f"runs/{digest}.json"
    summary = trace_summary(data)
    if not destination or "/" in destination:
        raise ValueError("Destination must be one bucket, container, or collection name.")
    if provider not in ("aws", "azure", "firebase"):
        raise ValueError("Choose aws, azure, or firebase.")
    if provider == "azure" and (not account_url or not account_url.startswith("https://")):
        raise ValueError("Azure requires --account-url https://ACCOUNT.blob.core.windows.net")
    plan = {"provider": provider, "destination": destination,
            "key": digest if provider == "firebase" else key,
            "content": "summary" if provider == "firebase" else "full trace",
            "dry_run": dry_run}
    if dry_run:
        return plan
    if provider == "aws":
        import boto3
        # Credentials come from the standard AWS chain (profile/SSO/IAM role).
        boto3.client("s3").put_object(Bucket=destination, Key=key, Body=payload,
                                     ContentType="application/json")
    elif provider == "azure":
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient, ContentSettings
        with DefaultAzureCredential() as credential:
            with BlobServiceClient(account_url=account_url, credential=credential) as service:
                service.get_blob_client(container=destination, blob=key).upload_blob(
                    payload, overwrite=True, content_settings=ContentSettings(content_type="application/json"))
    else:
        import firebase_admin
        from firebase_admin import firestore
        try:
            application = firebase_admin.get_app()
        except ValueError:
            application = firebase_admin.initialize_app()
        firestore.client(app=application).collection(destination).document(digest).set(summary)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--provider", required=True, choices=["aws", "azure", "firebase"])
    parser.add_argument("--destination", required=True, help="Existing bucket, container, or Firestore collection")
    parser.add_argument("--account-url", help="Azure storage account HTTPS endpoint")
    parser.add_argument("--dry-run", action="store_true", help="Validate and show target without uploading")
    args = parser.parse_args()
    try:
        plan = export_trace(args.trace, args.provider, args.destination, args.account_url, args.dry_run)
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
