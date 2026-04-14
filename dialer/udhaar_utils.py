from .models import Agent, Campaign, Lead
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


def store_campaigns_from_df(df):
    # Group by agent + segment → one campaign per group
    grouped = df.groupby(["agent__telecard_username", "customer__segment"])

    for (agent_username, segment), group_df in grouped:
        # Get or skip agent
        try:
            agent = Agent.objects.get(telecard_username=agent_username)
        except Agent.DoesNotExist:
            logger.warning("Agent not found: {}".format(agent_username))
            continue

        # Get or create campaign for this agent + segment
        campaign_id = "{}-{}-{}".format(
            agent_username, segment, timezone.now().strftime("%Y%m%d")
        )
        campaign = Campaign.objects.create(
            agent=agent,
            segment=segment,
            campaign_id=campaign_id,
            campaign_name="{} - {}".format(agent_username, segment),
            status="active"
        )

        logger.info("Created campaign: {}".format(campaign_id))

        # Create leads for this campaign
        leads_to_create = []
        for _, row in group_df.iterrows():
            leads_to_create.append(Lead(
                campaign=campaign,
                udhaar_lead_id=str(row["lead_id"]),
                phone_number=str(row["number"]),
                customer_name=str(row.get("name", "")),
                city=row.get("city", None),
                address=row.get("address", None),
                last_order_details={
                    "last_order_date": str(row.get("last_order_date_x", "")),
                    "last_order_status": str(row.get("last_order_status", "")),
                },
                month_gmv=row.get("order_value_this_month"),
                overall_gmv=row.get("order_value_to_date"),
                last_call_date=row.get("last_call_date_x") or None,
                status="pending",
            ))

        Lead.objects.bulk_create(leads_to_create, ignore_conflicts=True)
        logger.info("Created {} leads for campaign {}".format(len(leads_to_create), campaign_id))