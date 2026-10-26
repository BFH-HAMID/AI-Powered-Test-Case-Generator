"""Optional durable artifact persistence for AWS S3 deployments."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def upload_artifact(
    run_id: str,
    filename: str,
    body: bytes | str,
    content_type: str = "application/octet-stream",
) -> Optional[str]:
    """Upload a run artifact to configured S3 bucket and return its s3:// URI.

    Returns None if artifact storage is not configured. AWS credentials are resolved by boto3's
    standard credential chain (Lambda execution role in production); credentials are never stored
    in application code or environment files.
    """
    settings = get_settings()
    bucket = settings.artifact_bucket
    if not bucket:
        return None
    key = "/".join(
        part.strip("/")
        for part in [settings.artifact_prefix, run_id, filename]
        if part and part.strip("/")
    )
    try:
        import boto3

        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8") if isinstance(body, str) else body,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return f"s3://{bucket}/{key}"
    except Exception as exc:
        logger.exception("S3 artifact upload failed", extra={"run_id": run_id})
        raise RuntimeError(f"Unable to upload artifact to S3: {exc}") from exc


def upload_run_artifacts(run_id: str, test_code: str, metadata: Dict[str, Any]) -> Dict[str, str]:
    """Persist generated test code and JSON metadata to S3, returning artifact URIs."""
    uris: Dict[str, str] = {}
    code_uri = upload_artifact(
        run_id,
        "generated_tests.txt",
        test_code,
        content_type="text/plain; charset=utf-8",
    )
    metadata_uri = upload_artifact(
        run_id,
        "run_metadata.json",
        json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
        content_type="application/json",
    )
    if code_uri:
        uris["test_code"] = code_uri
    if metadata_uri:
        uris["metadata"] = metadata_uri
    return uris
