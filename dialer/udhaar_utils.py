from .models import Agent, Campaign, Lead, CallLog
from django.utils import timezone
import logging
import requests
from django.conf import settings
from voice_orchestrator.utils import normalize_phone_number
import time
import pandas as pd
from io import StringIO
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

UDHAAR_DISPOSITION_MAP = {
    'answered': 'ANSWERED',
    'failed': 'FAILED',
    'no_answer': 'NO ANSWER',
    'busy': 'BUSY',
    'lose_race': 'BUSY',
    'user_not_registered': 'FAILED',
    'invalid': 'FAILED',
    'cancelled': 'FAILED',
    }

UDHAAR_BASE_URL = "https://udhaar-api.oscar.pk/"


def _coerce_udhaar_lead_id(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_nullable_date(value):
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isnull(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def store_campaigns_from_df(df):
    # Group by agent + segment → one campaign per group
    grouped = df.groupby(["agent__telecard_username", "customer__segment"])

    for (agent_username, segment), group_df in grouped:
        # Get or skip agent
        if agent_username not in ['Rahim Qasim', "Sufiyan Shoukat", "anumzehra", "saadsaleem"]:
            logger.warning("Skipping unknown agent: {}".format(agent_username))
            continue
        
        try:
            agent = Agent.objects.get(telecard_username=agent_username)
        except Agent.DoesNotExist:
            logger.warning("Agent not found: {}".format(agent_username))
            continue

        # Get or create campaign for this agent + segment
        campaign_id = "{}-{}-{}".format(
            agent_username, segment, timezone.now().strftime("%Y%m%d")
        )
        if segment == 'growth-churn':
            segment = 'growth_churn'
        
        if segment == 'active-churn':
            segment = 'active_churn'

        campaign = Campaign.objects.create(
            agent=agent,
            segment=segment,
            campaign_id=campaign_id,
            campaign_name="{} - {}".format(agent_username, segment),
            campaign_type=Campaign.CAMPAIGN_TYPE_CHOICES[0][0]
        )

        logger.info("Created campaign: {}".format(campaign_id))

        # Create or update leads for this campaign by Udhaar lead id
        leads_to_create = []
        leads_to_create_by_udhaar_id = {}
        leads_to_update = []
        leads_to_update_by_id = {}
        for _, row in group_df.iterrows():
            udhaar_lead_id = _coerce_udhaar_lead_id(row["lead_id"])
            if udhaar_lead_id is None:
                logger.warning("Skipping row with invalid lead_id: {}".format(row.get("lead_id")))
                continue

            phone_number = normalize_phone_number(str(row["number"]))

            follow_up_date = row.get("follow_up_date")
            if pd.notnull(follow_up_date):
                follow_up_date = datetime.strptime(
                    follow_up_date,
                    "%Y-%m-%d"
                ).date()
            else:
                follow_up_date = None

            lead_defaults = {
                "campaign": campaign,
                "phone_number": phone_number,
                "customer_name": str(row.get("name", "")),
                "city": row.get("city", None),
                "address": row.get("address", None),
                "last_order_details": {
                    "last_order_date": str(row.get("last_order_date_x", "")),
                    "last_order_status": str(row.get("last_order_status", "")),
                },
                "month_gmv": row.get("order_value_this_month"),
                "overall_gmv": row.get("order_value_to_date"),
                "last_call_date": _coerce_nullable_date(row.get("last_call_date_x")),
                "status": "pending",
                "follow_up_date": str(follow_up_date) if follow_up_date else None,
                "comment": row.get("comment", None),
            }

            lead = Lead.objects.filter(udhaar_lead_id=udhaar_lead_id).order_by('-created_at').first()
            if lead:
                for field, value in lead_defaults.items():
                    setattr(lead, field, value)
                leads_to_update_by_id[lead.id] = lead
            elif udhaar_lead_id in leads_to_create_by_udhaar_id:
                lead = leads_to_create_by_udhaar_id[udhaar_lead_id]
                for field, value in lead_defaults.items():
                    setattr(lead, field, value)
            else:
                lead = Lead(
                    udhaar_lead_id=udhaar_lead_id,
                    **lead_defaults
                )
                leads_to_create.append(lead)
                leads_to_create_by_udhaar_id[udhaar_lead_id] = lead

        leads_to_update = list(leads_to_update_by_id.values())

        if leads_to_create:
            Lead.objects.bulk_create(leads_to_create)

        if leads_to_update:
            Lead.objects.bulk_update(
                leads_to_update,
                [
                    "campaign",
                    "phone_number",
                    "customer_name",
                    "city",
                    "address",
                    "last_order_details",
                    "month_gmv",
                    "overall_gmv",
                    "last_call_date",
                    "status",
                    "follow_up_date",
                    "comment",
                ]
            )

        logger.info(
            "Upserted leads for campaign {}: created {}, updated {}".format(
                campaign_id,
                len(leads_to_create),
                len(leads_to_update)
            )
        )

def refill_emi_campaign_data(agent_ids):
    agents = Agent.objects.filter(telecard_username__isnull=False, id__in=agent_ids).exclude(telecard_username='')
    telecard_usernames = list(agents.values_list('telecard_username', flat=True))

    if not telecard_usernames:
        logger.warning("refill_emi_campaign_data: no agents with telecard_username found")
        return {"success": False, "error": "no agents"}

    api_url = f"{UDHAAR_BASE_URL}telecard/emi-campaign-users/today/"
    headers = {"X-API-Key": settings.API_KEY}

    try:
        response = requests.post(api_url, json={"data": {"agents": telecard_usernames}}, headers=headers, timeout=60)
        response.raise_for_status()
        response_data = response.json()
    except Exception as exc:
        logger.exception("refill_emi_campaign_data: API call failed: {}".format(exc))
        return {"success": False, "error": str(exc)}

    if not response_data.get("success"):
        logger.error("refill_emi_campaign_data: API returned failure: {}".format(response_data))
        return {"success": False, "error": "API returned failure"}

    agent_by_username = {a.telecard_username: a for a in agents}
    total_created = 0

    for telecard_username, leads_data in response_data.get("data", {}).items():
        agent = agent_by_username.get(telecard_username)
        if not agent:
            logger.warning("refill_emi_campaign_data: no agent for username {}".format(telecard_username))
            continue

        campaign = Campaign.objects.filter(agent=agent, active=True).first()
        if not campaign:
            logger.error(f"No active campaign found for agent: {agent.user.get_full_name()}")
            continue

        leads_to_create = []
        for entry in leads_data:
            emi_id = entry.get("id")
            phone_number = normalize_phone_number(str(entry.get("phone_number", "")))
            stage = entry.get("stage")

            if not Lead.objects.filter(emi_id=emi_id, campaign=campaign).exists():
                leads_to_create.append(Lead(
                    campaign=campaign,
                    emi_id=emi_id,
                    phone_number=phone_number,
                    customer_name=phone_number,
                    emi_stage=stage,
                    status="pending",
                ))

        Lead.objects.bulk_create(leads_to_create, ignore_conflicts=True)
        total_created += len(leads_to_create)

    return {"success": True, "total_leads_created": total_created}


def fetch_verified_emi_phone_numbers(target_date=None):
    verified_date = target_date or (timezone.localdate() - timedelta(days=1))
    verified_date_value = verified_date.strftime("%Y-%m-%d")

    api_url = f"{UDHAAR_BASE_URL}telecard/emi-verified-users/"
    headers = {"X-API-Key": settings.API_KEY}
    payload = {"date": verified_date_value}

    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        response_data = response.json()
    except Exception as exc:
        logger.exception("fetch_verified_emi_phone_numbers: API call failed: {}".format(exc))
        return []

    if not response_data.get("success"):
        logger.error("fetch_verified_emi_phone_numbers: API returned failure: {}".format(response_data))
        return []

    verified_phones = response_data.get("data") or []
    if not isinstance(verified_phones, list):
        logger.error("fetch_verified_emi_phone_numbers: invalid data payload: {}".format(response_data))
        return []

    return verified_phones


def fetch_and_store_telebook_campaign():
    # return    
    MAX_TRIES = 10
    POLL_INTERVAL = 45  # seconds
    POLL_API_URL = "https://udhaar-api.oscar.pk/marketplace/telebook/campaigns/"
    headers = {"X-API-Key": settings.API_KEY}

    # Step 1: Trigger the campaign generation
    try:
        response = requests.post(POLL_API_URL, headers=headers)
        if not response.ok:
            logger.error("Trigger campaign HTTP {}: {}".format(response.status_code, response.text[:500]))
            return
        if not response.text.strip():
            logger.error("Trigger campaign returned empty body (HTTP {})".format(response.status_code))
            return
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

def sync_emi_converted_leads():
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