import json
from datetime import datetime
from django.utils import timezone

from dialer.models import CallLog


def generate_call_audio_reversed_json(start_date, end_date, output_file="call_logs.json"):
    """
    start_date_str, end_date_str format: 'YYYY-MM-DD'
    """

    start_date = timezone.make_aware(datetime.strptime(start_date, "%Y-%m-%d"))
    end_date = timezone.make_aware(datetime.strptime(end_date, "%Y-%m-%d"))

    # Step 1: Get ALL calls in range (no status filter)
    queryset = (
        CallLog.objects
        .filter(
            created_at__gte=start_date,
            created_at__lte=end_date
        )
        .order_by('to_number', 'created_at')
    )

    # Step 2: Group by (phone, day)
    groups = {}
    all_calls_by_group = {}

    for call in queryset:
        day = call.created_at.date()
        key = (call.to_number, day)

        if key not in groups:
            groups[key] = []
        groups[key].append(call)

    output = []

    # Step 3: For each group, pick latest call + compute flag
    for key, calls in groups.items():
        to_number, day = key

        # latest call in that day for that number
        latest_call = calls[-1]  # because ordered by created_at asc

        # condition: more than 1 call in same day for same number
        has_previous_same_day_call = len(calls) > 1

        # reversed logic (only for answered calls)
        audio_reversed = False
        if latest_call.status == 'answered' and has_previous_same_day_call:
            audio_reversed = True

        output.append({
            "uuid": latest_call.call_id,
            "audio_reversed": audio_reversed,
            "phone_number": to_number,
        })

    # Step 4: write JSON
    with open(output_file, "w") as f:
        json.dump(output, f, indent=4)

    return output