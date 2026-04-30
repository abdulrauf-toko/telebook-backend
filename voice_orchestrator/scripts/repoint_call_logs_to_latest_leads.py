import argparse
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "voice_orchestrator.settings")

import django

django.setup()

from django.db import transaction
from django.db.models import Count

from dialer.models import CallLog, Lead


def get_duplicate_udhaar_lead_ids():
    duplicate_groups = (
        Lead.objects
        .exclude(udhaar_lead_id__isnull=True)
        .values("udhaar_lead_id")
        .annotate(lead_count=Count("id"))
        .filter(lead_count__gt=1)
    )
    return [group["udhaar_lead_id"] for group in duplicate_groups]


def repoint_call_logs_to_latest_leads(execute=False):
    groups_checked = 0
    logs_to_update = 0
    logs_updated = 0

    with transaction.atomic():
        for udhaar_lead_id in get_duplicate_udhaar_lead_ids():
            lead_ids = list(
                Lead.objects
                .filter(udhaar_lead_id=udhaar_lead_id)
                .order_by("-created_at", "-id")
                .values_list("id", flat=True)
            )
            if len(lead_ids) < 2:
                continue

            groups_checked += 1
            latest_lead_id = lead_ids[0]
            duplicate_lead_ids = lead_ids[1:]
            call_logs = CallLog.objects.filter(lead_id__in=duplicate_lead_ids)
            update_count = call_logs.count()
            logs_to_update += update_count

            if execute and update_count:
                logs_updated += call_logs.update(lead_id=latest_lead_id)

            print(
                "udhaar_lead_id={} latest_lead_id={} duplicate_lead_ids={} call_logs={}".format(
                    udhaar_lead_id,
                    latest_lead_id,
                    duplicate_lead_ids,
                    update_count,
                )
            )

        if not execute:
            transaction.set_rollback(True)

    print(
        "{} groups checked. {} call logs {}.".format(
            groups_checked,
            logs_to_update if not execute else logs_updated,
            "would be updated" if not execute else "updated",
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Repoint CallLog.lead from duplicate leads to the most recent Lead for each udhaar_lead_id."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist updates. Without this flag, the script only reports what would change.",
    )
    args = parser.parse_args()

    repoint_call_logs_to_latest_leads(execute=args.execute)


if __name__ == "__main__":
    main()
