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


def delete_duplicate_leads(execute=False, force=False):
    groups_checked = 0
    leads_to_delete = 0
    leads_deleted = 0
    skipped_groups = 0

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
            referenced_call_logs = CallLog.objects.filter(lead_id__in=duplicate_lead_ids).count()

            if referenced_call_logs and not force:
                skipped_groups += 1
                print(
                    "SKIPPED udhaar_lead_id={} latest_lead_id={} duplicate_lead_ids={} referenced_call_logs={}".format(
                        udhaar_lead_id,
                        latest_lead_id,
                        duplicate_lead_ids,
                        referenced_call_logs,
                    )
                )
                continue

            delete_count = len(duplicate_lead_ids)
            leads_to_delete += delete_count

            if execute and delete_count:
                deleted_count, _ = Lead.objects.filter(id__in=duplicate_lead_ids).delete()
                leads_deleted += deleted_count

            print(
                "udhaar_lead_id={} keeping_latest_lead_id={} deleting_duplicate_lead_ids={} referenced_call_logs={}".format(
                    udhaar_lead_id,
                    latest_lead_id,
                    duplicate_lead_ids,
                    referenced_call_logs,
                )
            )

        if not execute:
            transaction.set_rollback(True)

    print(
        "{} groups checked. {} groups skipped. {} duplicate leads {}.".format(
            groups_checked,
            skipped_groups,
            leads_to_delete if not execute else leads_deleted,
            "would be deleted" if not execute else "deleted",
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Delete duplicate Lead rows, keeping the most recent Lead for each udhaar_lead_id."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist deletes. Without this flag, the script only reports what would change.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete duplicates even if call logs still reference them. Prefer running the repoint script first.",
    )
    args = parser.parse_args()

    delete_duplicate_leads(execute=args.execute, force=args.force)


if __name__ == "__main__":
    main()
