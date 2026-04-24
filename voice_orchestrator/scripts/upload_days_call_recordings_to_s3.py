import os
from datetime import date, datetime, time

from django.utils import timezone

from dialer.models import CallLog
from events.tasks import upload_call_recording_to_s3


RECORDINGS_ROOT = "/home/pbx/telebook-pbx/recordings"


def _coerce_to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d").date()
    raise ValueError("start_date and end_date must be date, datetime, or YYYY-MM-DD strings")


def _resolve_recording_path(recording_value: str) -> str:
    if not recording_value:
        return None

    if recording_value.startswith(("http://", "https://")):
        return None

    if os.path.isabs(recording_value):
        return recording_value

    return os.path.join(RECORDINGS_ROOT, recording_value.lstrip("/"))


def upload_days_call_recordings_to_s3(start_date, end_date):
    start_date = _coerce_to_date(start_date)
    end_date = _coerce_to_date(end_date)

    if start_date > end_date:
        raise ValueError("start_date cannot be greater than end_date")

    start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
    end_dt = timezone.make_aware(datetime.combine(end_date, time.max))

    call_logs = CallLog.objects.filter(
        initiated_at__gte=start_dt,
        initiated_at__lte=end_dt,
    ).exclude(recording_url__isnull=True).exclude(recording_url="")

    queued_count = 0

    print(f"Found {call_logs.count()} call logs with recordings between {start_date} and {end_date}. Starting upload...")
    
    for log in call_logs.iterator():
        recording_path = _resolve_recording_path(log.recording_url)
        if recording_path and os.path.exists(recording_path):
            upload_call_recording_to_s3(log.id, recording_path)
            queued_count += 1

    return queued_count
