"""
Events Tasks - FreeSWITCH Event Processing

Handles asynchronous processing of ESL events from FreeSWITCH.
Events are processed in the default queue (parallel processing).
"""

import logging
from datetime import datetime
from typing import Dict, Optional
from CELERY_INIT import app
from django.utils import timezone
from django.conf import settings
from dialer.models import CallLog, Agent, Lead
from voice_orchestrator.redis import SYNC_TO_DB_LOCK_REDIS_KEY, conn
from .utils import connect_agent_to_call, disconnect_call, is_agent_idle_in_cache, mark_agent_busy_in_cache, sync_to_db_wrapper, transfer_call, update_active_call_in_cache, bridge_agent_to_call, mark_agent_idle_in_cache, map_call_status, call_ending_routine, handle_free_agent, add_customer_to_waiting_queue, remove_customer_from_waiting_queue, get_next_customer_waiting_in_queue
from dialer.utils import get_next_available_secondary_sales_agent, remove_active_call, construct_queue_object, add_to_priority_queue_mapping, add_call_to_completed_list, get_and_clear_completed_calls, get_next_available_support_agent, get_next_available_sales_agent
logger = logging.getLogger('events')

# ============================================================================
# MAIN EVENT PROCESSING ENTRY POINT
# ============================================================================

import logging
import time
import redis

from celery import shared_task
from greenswitch import InboundESL

logger = logging.getLogger(__name__)

REDIS_LOCK_KEY = "esl_listener_lock"
REDIS_LOCK_TTL = 10

@shared_task(bind=True)
def start_esl_listener(self):
    while True:
        try:
            fs = InboundESL(
                host=settings.FREESWITCH_ESL_HOST,
                port=settings.FREESWITCH_ESL_PORT,
                password=settings.FREESWITCH_ESL_PASSWORD
            )

            fs.connect()

            fs.send('event plain CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE CHANNEL_PARK CHANNEL_EXECUTE')

            fs.register_handle('*', dispatch_event_handler)

            logger.info("Freeswitch Connected. Listening for events...")

            while fs.connected:
                fs.process_events()

        except Exception as e:
            logger.exception("ESL connection lost. Reconnecting in 1s...")
            time.sleep(1)


def dispatch_event_handler(event) -> str:
    try:
        event_type = event.type
        event_id = event.id
        direction = event.getHeader("Call-Direction")
        other_leg_uuid = event.getHeader("Other-Leg-Unique-ID", None)
        caller_id_number = event.getHeader("Caller-Caller-ID-Number", None) #NOT SURE IF THIS IS THE CORRECT ONE. CONFIRM LATER
        variable_uuid = event.getHeader("variable_uuid") #call_uuid
        auto_bridge = event.getHeader("variable_sip_h_X-auto_bridge", None)
        agent_id = event.getHeader("variable_sip_h_X-agent_id", None)
        
        if event_type == 'CHANNEL_ANSWER':
            if direction == 'outbound':
                if not other_leg_uuid: #first answer
                    if not auto_bridge: #lead picked up first
                        if agent_id: #if agent is assigned to this lead, check if idle and connect. all leads except aquisition 
                            if is_agent_idle_in_cache(agent_id, check_call_id=True, check_state=False):
                                connect_agent_to_call(agent_id, variable_uuid)
                            else: #if not idle, disconnect call and add lead back to queue
                                disconnect_call(variable_uuid, cause="AGENT_BUSY")
                                # call_details = remove_active_call(variable_uuid)
                                # add_to_priority_queue_mapping(agent_id, call_details.get('payload', None))  handling this after call ends
                        else: #aquisition calls with no agents
                            agent_id = get_next_available_sales_agent()
                            if not agent_id:
                                agent_id = get_next_available_secondary_sales_agent()
                            if agent_id:
                                connect_agent_to_call(agent_id, variable_uuid)
                            else:
                                disconnect_call(variable_uuid, cause="NO_AVAILABLE_AGENT")
                                # call_details = remove_active_call(variable_uuid)
                                # add_to_priority_queue_mapping(agent_id='0', entry=call_details.get('payload', None)) #agent id 0 indicates no specific agent is assigned

                else:
                    update_active_call_in_cache(variable_uuid, {"connected_at": time.time()})
            else:
                # this case is for inbound pick ups. Since IVR is handled by freeswtich, we only connect the agent at park event 
                pass

        elif event_type == 'CHANNEL_EXECUTE':
            application = event.get('Application')

            if application == 'transfer':
                uuid = event.get('Unique-ID')
                # The new destination Agent B
                new_destination = event.get('Application-Data') 
                # The agent who did the transferring
                transferor = event.get('variable_last_sent_callee_id_number')
                # Try these in order to find Agent A
                # transferor = (
                #     event.get('variable_last_sent_callee_id_number') or 
                #     event.get('variable_caller_id_number') or 
                #     event.get('variable_origination_caller_id_number')
                # )
                mark_agent_idle_in_cache(transferor) 
                mark_agent_busy_in_cache(new_destination)

        elif event_type == 'CHANNEL_PARK':
            if direction == 'inbound':
                selection = event.get('variable_ivr_choice')
                uuid = event.get('Unique-ID')
                if selection == "1": #support
                    agent_id = get_next_available_support_agent()
                elif selection == "2": #sales
                    agent_id = get_next_available_secondary_sales_agent()
                else:
                    logger.error('invalid ivr choice')
                    return
                if agent_id:
                    mark_agent_busy_in_cache(agent_id, uuid)
                    transfer_call(uuid, agent_id)
                else:
                    team = 'support' if selection == '1' else 'secondary_sales'
                    add_customer_to_waiting_queue(uuid, team)
                    transfer_call(uuid, "waiting_room") #make sure this exists in freeswitch config 
                    
        
        elif event_type == 'CHANNEL_HANGUP_COMPLETE':
            hangup_cause = event.get('variable_hangup_cause')
            call_details = remove_active_call(variable_uuid)
            if not agent_id:
                agent_id = call_details.get('agent_id', None) if call_details else None
            
            if hangup_cause in ['NO_AVAILABLE_AGENT', 'AGENT_BUSY', 'LOSE_RACE']:
                if call_details and call_details.get('payload', None):
                    add_to_priority_queue_mapping(agent_id, call_details)

            if hangup_cause in ['BLIND_TRANSFER', 'ATTENDED_TRANSFER', 'TRANSFER']:
                transferor_ext = (
                    event.get('variable_user_name') or 
                    event.get('Caller-Caller-ID-Number') or 
                    event.get('variable_origination_caller_id_number')
                )
                # Mark the transferor agent as idle
                #TODO probably handled in transfer

            if hangup_cause == 'NORMAL_CLEARING':
                if agent_id:
                    handle_free_agent(agent_id)
            
            call_ending_routine(call_details, event, direction)
        else:
            logger.debug(f"No specific handler for event type: {event_type}")
            
    except Exception as exc:
        logger.exception(f"Error dispatching event handler: {exc}")
        return f'ERROR: {str(exc)}'

@app.task(bind=True)
def add_lead_back_to_queue(self, lead_id):
    try:
        lead = Lead.objects.get(id=lead_id)
        queue_object = construct_queue_object(lead.campaign, lead)
        add_to_priority_queue_mapping(lead.campaign.agent_id, queue_object)
    except Lead.DoesNotExist:
        logger.error(f"Lead with id {lead_id} does not exist.")

    

@app.task(bind=True)
def sync_to_db(self):
    call_list = get_and_clear_completed_calls()
    call_completed_lead_ids = []
    call_not_picked_lead_ids = []
    invalid_lead_ids = []
    try: 
        for call_details in call_list:
            payload = call_details.get('payload', {})
            lead_id = payload.get('lead_id')
            if not lead_id:
                logger.warning(f"No lead_id found in call payload for call_id {call_details.get('call_uuid', None)}. Skipping DB sync for this call.")
                continue

            disconnect_reason = call_details.get('disconnect_reason')
            initiated_at = call_details.get('initiated_at', None)
            connected_at = call_details.get('connected_at', None)
            ended_at = call_details.get('ended_at', None)
            duration_seconds = call_details.get('duration_seconds', None)
            direction = call_details.get('direction')

            agent_id = call_details.get('agent_id', None)
            phone_number = call_details.get('to_number', None)
            call_status = map_call_status(disconnect_reason)
            call_uuid = call_details.get('call_uuid')
            call_log = CallLog.objects.create(
                call_id=call_uuid,
                agent_id=agent_id,
                lead_id=lead_id,
                to_number=phone_number,
                status=call_status,
                disconnect_reason=disconnect_reason,
                initiated_at=datetime.fromtimestamp(initiated_at) if initiated_at else None,
                answered_at=datetime.fromtimestamp(connected_at) if connected_at else None,
                ended_at=datetime.fromtimestamp(ended_at) if ended_at else None,
                duration_seconds=duration_seconds,
                recording_url=call_details.get('recording_url', ''),
                recording_stored=call_details.get('recording_stored', False),
                direction=direction,
            )

            if call_status in ['answered', 'no_answer', 'busy', 'invalid']:
                if call_status == 'answered':
                    call_completed_lead_ids.append(lead_id)
                elif call_status in ['no_answer', 'busy']:
                    call_not_picked_lead_ids.append(lead_id)
                elif call_status == 'invalid':
                    invalid_lead_ids.append(lead_id)
            else:
                logger.warning(f"Call with call_id {call_details.get('call_uuid')} has unrecognized hangup cause: {call_details.get('disconnect_reason')}")
            
            logger.info(f"Call log created for call_id: {call_log.call_id}")

        completed_leads = Lead.objects.filter(id__in=call_completed_lead_ids).update(status='completed', last_call_date=timezone.now())
        not_picked_leads = Lead.objects.filter(id__in=call_not_picked_lead_ids).update(status='not_answered', last_call_date=timezone.now())
        invalid_leads = Lead.objects.filter(id__in=invalid_lead_ids).update(status='invalid', last_call_date=timezone.now())
        conn.delete(SYNC_TO_DB_LOCK_REDIS_KEY)
        logger.info(f"Updated {completed_leads} leads to completed, {not_picked_leads} leads to not_answered, {invalid_leads} leads to invalid based on call outcomes.")

    except Exception as e:
        logger.exception(f"Error syncing call log to DB for call_id {call_details.get('call_id')}: {e}")


@app.task
def waiting_room_task():
    while True:
        try:
            # Check support team waiting queue
            support_customer = get_next_customer_waiting_in_queue('support')
            if support_customer:
                agent_id = get_next_available_support_agent()
                if agent_id:
                    mark_agent_busy_in_cache(agent_id, support_customer)
                    connect_agent_to_call(agent_id, support_customer)
                    remove_customer_from_waiting_queue(support_customer)
                    logger.info(f"Connected support customer {support_customer} to agent {agent_id}")

            # Check secondary_sales team waiting queue
            secondary_sales_customer = get_next_customer_waiting_in_queue('secondary_sales')
            if secondary_sales_customer:
                agent_id = get_next_available_secondary_sales_agent()
                if agent_id:
                    mark_agent_busy_in_cache(agent_id, secondary_sales_customer)
                    connect_agent_to_call(agent_id, secondary_sales_customer)
                    remove_customer_from_waiting_queue(secondary_sales_customer)
                    logger.info(f"Connected secondary_sales customer {secondary_sales_customer} to agent {agent_id}")

            time.sleep(2) # to reduce cpu usage
        except Exception as e:
            logger.exception(f"Error in waiting_room_task: {e}")
            time.sleep(3)
