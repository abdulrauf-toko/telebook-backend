from .models import Agent, Campaign, Lead, CallLog
from django.utils import timezone
import logging
from voice_orchestrator.utils import normalize_phone_number
from collections import defaultdict

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
            status="active"
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
                "last_call_date": row.get("last_call_date_x") or None,
                "status": "pending",
                "follow_up_date": row.get("follow_up_date", None),
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


def store_campaigns_from_csv(csv_path):
    import csv
    from django.utils import timezone

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Group by name + segment
    grouped = {}
    for row in rows:
        key = (row['name'], row['segment'])
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(row)

    for (name, segment), group_rows in grouped.items():
        # Get or create campaign for this name + segment
        campaign_id = "{}-{}-{}".format(
            name, segment, timezone.now().strftime("%Y%m%d")
        )

        agent = Agent.objects.get(telecard_username=name)
        campaign = Campaign.objects.create(
            agent=agent,
            segment='acquisition',
            campaign_id=campaign_id,
            campaign_name="{} - {}".format(name, segment),
            status="active"
        )
        logger.info("Created campaign: {}".format(campaign_id))

        # Create leads for this campaign
        leads_to_create = []
        for row in group_rows:
            leads_to_create.append(Lead(
                campaign=campaign,
                udhaar_lead_id=row['dukaan_account_id'],
                phone_number=str(row['number']),
                customer_name=str(row['name']),
                status="pending",
            ))

        Lead.objects.bulk_create(leads_to_create, ignore_conflicts=True)
        logger.info("Created {} leads for campaign {}".format(len(leads_to_create), campaign_id))


def get_emi_call_logs(date):
    
    all_logs = (
        CallLog.objects
        .filter(initiated_at__date=date)
        .select_related('agent', 'lead', 'lead__campaign')
        .order_by('to_number', '-initiated_at')
    )

    # Group by phone number, preserving -initiated_at order (most recent first)
    logs_by_number = defaultdict(list)
    for log in all_logs:
        logs_by_number[log.to_number].append(log)

    # If any log for a number has LOSE_RACE or USER_NOT_REGISTERED, keep only the most recent
    SKIP_REASONS = {'LOSE_RACE', 'USER_NOT_REGISTERED'}
    filtered_logs = []
    for logs in logs_by_number.values():
        if len(logs) > 1 and any(log.disconnect_reason in SKIP_REASONS for log in logs):
            filtered_logs.append(logs[0])
        else:
            filtered_logs.extend(logs)

    # Batch-fetch the latest lead for phone numbers where no lead is attached
    no_lead_phones = {log.to_number for log in filtered_logs if log.lead is None}
    lead_by_phone = {}
    if no_lead_phones:
        leads_qs = (
            Lead.objects
            .filter(phone_number__in=no_lead_phones)
            .select_related('campaign')
            .order_by('phone_number', '-created_at')
        )
        for lead in leads_qs:
            if lead.phone_number not in lead_by_phone:
                lead_by_phone[lead.phone_number] = lead

    data = {}
    for log in filtered_logs:
        lead = log.lead if log.lead is not None else lead_by_phone.get(log.to_number)

        if lead is None or lead.campaign is None or lead.campaign.segment != 'other':
            continue

        if not lead.emi_id:
            continue

        data[str(lead.emi_id)] = {
            'called_at': log.initiated_at.isoformat() if log.initiated_at else None,
            'disposition': UDHAAR_DISPOSITION_MAP.get(log.status),
        }

    logger.info("get_emi_call_logs: built report for {} leads".format(len(data)))
    return {'data': data}