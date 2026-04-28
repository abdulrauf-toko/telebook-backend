from .models import Agent, Campaign, Lead
from django.utils import timezone
import logging
from voice_orchestrator.utils import normalize_phone_number

logger = logging.getLogger(__name__)


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
