import logging
from django.utils import timezone
from voice_orchestrator.celery import app
import time
from .utils import construct_queue_object, flush_redis_data, make_campaigns_inactive, add_to_priority_queue_mapping, \
        validate_and_cleanup_agent_states, check_and_refill_queue, acquire_dialer_lock, process_priority_queue, \
        process_secondary_queue, release_dialer_lock, reset_selected_campaign
from .models import Lead, Campaign, Agent, CallLog
from django.db import transaction
from events.utils import get_all_idle_agents_in_cache
from .udhaar_utils import store_campaigns_from_df
from voice_orchestrator.utils import normalize_phone_number
import requests
import pandas as pd
from io import StringIO
from django.conf import settings
from dialer.udhaar_utils import UDHAAR_BASE_URL

logger = logging.getLogger(__name__)

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
def fetch_and_store_telebook_campaign(self):
    return    
    MAX_TRIES = 10
    POLL_INTERVAL = 45  # seconds
    POLL_API_URL = "https://udhaar-api.oscar.pk/marketplace/telebook/campaigns/"
    headers = {"X-API-Key": settings.API_KEY}

    # Step 1: Trigger the campaign generation
    try:
        response = requests.post(POLL_API_URL, headers=headers)
        data = response.json()
        if not data.get("success"):
            logger.error("Failed to trigger campaign: {}".format(data))
            return
        poll_id = data["poll_id"]
        logger.info("Campaign triggered, poll_id: {}".format(poll_id))
    except Exception as e:
        logger.exception("Error triggering campaign: {}".format(e))
        return

    # Step 2: Poll until completed
    for attempt in range(1, MAX_TRIES + 1):
        logger.info("Polling attempt {}/{}".format(attempt, MAX_TRIES))
        time.sleep(POLL_INTERVAL)

        try:
            poll_response = requests.get(
                POLL_API_URL,
                headers=headers,
                params={"poll_id": poll_id}
            )

            # CSV is returned when completed
            if poll_response.headers.get("Content-Type") == "text/csv":
                logger.info("CSV received, processing...")
                csv_content = poll_response.content.decode("utf-8")
                df = pd.read_csv(StringIO(csv_content))
                store_campaigns_from_df(df)
                return

            result = poll_response.json()
            status = result.get("status")

            if status == "pending":
                logger.info("Still pending...")
                continue
            elif status == "failed":
                logger.error("Campaign generation failed on server side")
                return

        except Exception as e:
            logger.exception("Error polling: {}".format(e))
            continue

    logger.error("Max polling attempts ({}) reached without completion".format(MAX_TRIES))


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

