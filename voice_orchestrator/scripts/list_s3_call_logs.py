import os

import boto3


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "voice_orchestrator.settings")

import django

django.setup()

from django.conf import settings


def list_s3_call_logs():
    bucket_name = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if not bucket_name:
        raise RuntimeError(
            "AWS_STORAGE_BUCKET_NAME is not set. Export it in your environment or add it to your .env file."
        )

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )

    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(
        Bucket=bucket_name,
        Prefix="call-logs/",
    )

    found_any = False
    for page in pages:
        for item in page.get("Contents", []):
            found_any = True
            print(item["Key"])

    if not found_any:
        print("No files found under call-logs/.")


if __name__ == "__main__":
    list_s3_call_logs()
