import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from urllib.parse import unquote, urlparse
from django.utils import timezone
from voice_orchestrator.celery import app
from .utils import construct_queue_object, flush_redis_data, make_campaigns_inactive, add_to_priority_queue_mapping, \
        validate_and_cleanup_agent_states, check_and_refill_queue, acquire_dialer_lock, process_priority_queue, \
        process_secondary_queue, release_dialer_lock, reset_selected_campaign
from .models import Lead, Campaign, Agent, CallLog, CallLogExports
from django.db import transaction
from django.db.models import Q
from events.utils import get_all_idle_agents_in_cache
from voice_orchestrator.utils import normalize_phone_number
import requests
import pytz
from django.conf import settings
from dialer.udhaar_utils import UDHAAR_BASE_URL, fetch_verified_emi_phone_numbers

logger = logging.getLogger(__name__)
CALL_LOG_EXPORT_ROOT = "/home/pbx/call_log_exports"
PKT = pytz.timezone("Asia/Karachi")

# ============================================================================
# DIALER ORCHESTRATION - MAIN ENTRY POINT
# ============================================================================

@app.task(bind=True)
def initiate_dialer_cycle(self):
    try:
        # Step 0: Acquire execution lock to prevent concurrent runs
        if not acquire_dialer_lock():
            logger.info("Dialer already in execution, skipping cycle")
            return {
                'status': 'skipped',
                'reason': 'dialer_locked',
                'timestamp': timezone.now().isoformat()
            }
        

        logger.info("=== DIALER CYCLE START ===")
        cycle_start = timezone.now()
        validate_and_cleanup_agent_states() #cleanup before processing to ensure we have the most accurate agent states. 

        
        # Step 1: Calculate effective agent capacity
        agent_capacity = len(get_all_idle_agents_in_cache(check_call_id=True, check_state=True))
        
        if agent_capacity <= 0:
            return {
                'status': 'skipped',
                'reason': 'no_agents_available',
                'timestamp': cycle_start.isoformat()
            }
        
        # Step 2: Process priority queue
        priority_dialed = process_priority_queue()
        agent_capacity -= priority_dialed
        
        # logger.info(f"Priority queue processed: {priority_dialed} calls") 
        
        # Step 3: Process secondary queue (predictive dialing)
        secondary_dialed = process_secondary_queue()
        
        # aquisition_dialed = process_aquisition_queue()
        # logger.info(f"Aquisition queue processed: {aquisition_dialed} calls")

        # Step 4: Check and refill queues if needed
        check_and_refill_queue()
        
        cycle_duration = (timezone.now() - cycle_start).total_seconds()
        total_calls_dialed = secondary_dialed
        
        metrics = {
            'timestamp': cycle_start.isoformat(),
            'duration_seconds': cycle_duration,
            'agent_capacity': agent_capacity,
            'priority_calls_dialed': priority_dialed,
            'secondary_calls_dialed': secondary_dialed,
            'total_calls_dialed': total_calls_dialed
        }
        
        logger.info(f"Dialer Cycle Metrics: \n{metrics}")
            
    except Exception as exc:
        logger.exception(f"Error in dialer cycle: {exc}")
        
        return {
            'status': 'error',
            'error': str(exc),
            'timestamp': timezone.now().isoformat()
        }
    
    finally:
        release_dialer_lock()


@app.task(bind=True)
def fetch_and_store_emi_campaigns(self):
    api_url = f"{UDHAAR_BASE_URL}telecard/emi-campaign-users/today/"
    headers = {"X-API-Key": settings.API_KEY}

    try:
        response = requests.get(api_url, headers=headers, timeout=60)
        response.raise_for_status()
        response_data = response.json()
    except Exception as exc:
        logger.exception("Error fetching EMI campaigns: {}".format(exc))
        return {
            "success": False,
            "message": "Error fetching EMI campaigns",
        }

    if not response_data.get("success"):
        logger.error("EMI campaigns API returned failure: {}".format(response_data))
        return {
            "success": False,
            "message": "EMI campaigns API returned failure",
        }

    campaigns_data = response_data.get("data") or {}
    today = timezone.now().strftime("%Y%m%d")
    created_count = 0
    updated_count = 0
    stale_deleted_count = 0
    stale_kept_count = 0
    skipped_agent_count = 0
    skipped_lead_count = 0

    for agent_username, leads_data in campaigns_data.items():
        try:
            agent = Agent.objects.get(telecard_username=agent_username)
        except Agent.DoesNotExist:
            skipped_agent_count += 1
            logger.warning("Skipping EMI campaign for unknown agent: {}".format(agent_username))
            continue

        campaign_id = "{}-emi-{}".format(agent_username, today)
        campaign, _ = Campaign.objects.get_or_create(
            campaign_id=campaign_id,
            defaults={
                "agent": agent,
                "segment": "other",
                "campaign_name": "{} - EMI".format(agent_username),
                "active": True,
            },
        )

        incoming_lead_ids = set()

        with transaction.atomic():
            for lead_data in leads_data or []:
                emi_id = lead_data.get("id")
                phone_number = normalize_phone_number(lead_data.get("phone_number"))

                if emi_id is None or not phone_number:
                    skipped_lead_count += 1
                    logger.warning("Skipping invalid EMI lead payload: {}".format(lead_data))
                    continue

                try:
                    emi_id = int(float(emi_id))
                except (TypeError, ValueError):
                    skipped_lead_count += 1
                    logger.warning("Skipping EMI lead with invalid id: {}".format(lead_data))
                    continue

                incoming_lead_ids.add(emi_id)

                lead = Lead.objects.filter(emi_id=emi_id).order_by("-created_at", "-id").first()
                if lead:
                    lead.campaign = campaign
                    lead.phone_number = phone_number
                    lead.customer_name = lead.customer_name or phone_number
                    if not CallLog.objects.filter(lead=lead).exists():
                        lead.status = "pending"

                    lead.save()
                    updated_count += 1
                else:
                    Lead.objects.create(
                        campaign=campaign,
                        emi_id=emi_id,
                        phone_number=phone_number,
                        customer_name=phone_number,
                        status="pending",
                    )
                    created_count += 1

            stale_leads = campaign.leads.exclude(emi_id__in=incoming_lead_ids)
            for stale_lead in stale_leads:
                if CallLog.objects.filter(lead=stale_lead).exists():
                    stale_kept_count += 1
                    continue

                stale_lead.delete()
                stale_deleted_count += 1

    result = {
        "success": True,
        "created": created_count,
        "updated": updated_count,
        "stale_deleted": stale_deleted_count,
        "stale_kept_with_call_logs": stale_kept_count,
        "skipped_agents": skipped_agent_count,
        "skipped_leads": skipped_lead_count,
    }
    logger.info("EMI campaigns sync complete: {}".format(result))
    return result


@app.task(bind=True)
def sync_emi_converted_leads(self):
    verified_phones = fetch_verified_emi_phone_numbers()
    normalized_phones = {
        normalize_phone_number(str(phone))
        for phone in verified_phones
        if phone
    }
    normalized_phones.discard(None)
    normalized_phones.discard("")

    if not normalized_phones:
        result = {
            "success": True,
            "total_phones": 0,
            "updated": 0,
        }
        logger.info("EMI converted sync complete: {}".format(result))
        return result

    latest_lead_ids = (
        Lead.objects
        .filter(phone_number__in=normalized_phones)
        .order_by("phone_number", "-created_at", "-id")
        .distinct("phone_number")
        .values_list("id", flat=True)
    )
    updated_count = Lead.objects.filter(id__in=latest_lead_ids).update(emi_converted=True)

    result = {
        "success": True,
        "total_phones": len(normalized_phones),
        "updated": updated_count,
    }
    logger.info("EMI converted sync complete: {}".format(result))
    return result


def _sanitize_export_part(value):
    value = str(value or "unknown").strip()
    value = re.sub(r"[^\w\s\-.]", "", value)
    value = re.sub(r"\s+", "_", value)
    return value or "unknown"


def _parse_s3_url(url):
    parsed = urlparse(url)
    bucket = parsed.netloc.split(".s3.")[0]
    key = unquote(parsed.path.lstrip("/"))
    return bucket, key


def _s3_client():
    import boto3
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )


def _apply_call_log_export_filters(filters):
    call_logs = (
        CallLog.objects
        .select_related("agent__user", "lead")
        .prefetch_related("agent__teams")
        .filter(recording_url__startswith="https://")
    )

    if filters.get("start_utc") and filters.get("end_utc"):
        start_utc = datetime.fromisoformat(filters["start_utc"])
        end_utc = datetime.fromisoformat(filters["end_utc"])
        call_logs = call_logs.filter(initiated_at__gte=start_utc, initiated_at__lte=end_utc)

    if filters.get("agent"):
        call_logs = call_logs.filter(agent_id=filters["agent"])

    if filters.get("team"):
        call_logs = call_logs.filter(agent__teams__id=filters["team"])

    if filters.get("emi_converted") == "yes":
        call_logs = call_logs.filter(lead__emi_converted=True)
    elif filters.get("emi_converted") == "no":
        call_logs = call_logs.filter(Q(lead__emi_converted=False) | Q(lead__emi_converted__isnull=True))

    sort_options = {
        "datetime_desc": "-initiated_at",
        "datetime_asc": "initiated_at",
        "duration_desc": "-duration_seconds",
        "duration_asc": "duration_seconds",
    }
    return call_logs.order_by(sort_options.get(filters.get("sort_by"), "-initiated_at")).distinct()


@app.task(bind=True)
def export_call_log_recordings_task(self, export_id):
    export = CallLogExports.objects.get(id=export_id)
    export.status = "running"
    export.error = None
    export.save(update_fields=["status", "error", "updated_at"])

    work_dir = os.path.join(CALL_LOG_EXPORT_ROOT, "work", str(export.id))

    try:
        os.makedirs(work_dir, exist_ok=True)
        os.makedirs(CALL_LOG_EXPORT_ROOT, exist_ok=True)

        call_logs = _apply_call_log_export_filters(export.filters)
        total = call_logs.count()
        export.total_recordings = total
        export.exported_recordings = 0
        export.save(update_fields=["total_recordings", "exported_recordings", "updated_at"])

        zip_path = os.path.join(CALL_LOG_EXPORT_ROOT, f"call_log_export_{export.id}.zip")
        s3_client = _s3_client()
        exported_count = 0

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for log in call_logs.iterator(chunk_size=200):
                agent_name = "No_agent"
                if log.agent and log.agent.user:
                    agent_name = log.agent.user.get_full_name() or log.agent.user.username
                elif log.agent:
                    agent_name = f"ext_{log.agent.extension}"

                call_date = log.initiated_at.astimezone(PKT).date().isoformat() if log.initiated_at else "unknown_date"
                agent_folder = _sanitize_export_part(agent_name)
                phone = _sanitize_export_part(log.lead.phone_number if log.lead and log.lead.phone_number else log.to_number)
                lead_stage = _sanitize_export_part(log.lead.emi_stage if log.lead and log.lead.emi_stage else log.status)
                original_filename = _sanitize_export_part(os.path.basename(urlparse(log.recording_url).path) or f"{log.call_id}.mp3")
                filename = f"{agent_folder}_{phone}_{lead_stage}_{original_filename}"
                local_path = os.path.join(work_dir, f"{log.id}_{filename}")
                archive_path = os.path.join(call_date, agent_folder, filename)

                bucket, key = _parse_s3_url(log.recording_url)
                s3_client.download_file(bucket, key, local_path)
                zip_file.write(local_path, archive_path)
                exported_count += 1

                if exported_count % 5 == 0 or exported_count == total:
                    CallLogExports.objects.filter(id=export.id).update(exported_recordings=exported_count)

        export.filepath = zip_path
        export.status = "completed"
        export.exported_recordings = exported_count
        export.save(update_fields=["filepath", "status", "exported_recordings", "updated_at"])
        return {"success": True, "export_id": export.id, "filepath": zip_path, "exported": exported_count}

    except Exception as exc:
        logger.exception("Error exporting call log recordings: {}".format(exc))
        export.status = "failed"
        export.error = str(exc)
        export.save(update_fields=["status", "error", "updated_at"])
        return {"success": False, "export_id": export.id, "error": str(exc)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.task
def day_end_routine():
    make_campaigns_inactive()
    reset_selected_campaign()
    flush_redis_data()

@app.task
def formdata_scheduled_task(lead_id):
    lead = Lead.objects.get(id=lead_id)
    queue_object = construct_queue_object(lead.campaign, lead)
    add_to_priority_queue_mapping(lead.campaign.agent_id, queue_object)
