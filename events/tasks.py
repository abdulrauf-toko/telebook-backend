"""
Events Tasks - FreeSWITCH Event Processing

Handles asynchronous processing of ESL events from FreeSWITCH.
Events are processed in the default queue (parallel processing).
"""

import logging
from datetime import date, datetime
from voice_orchestrator.celery import app
from django.utils import timezone
from django.conf import settings
from dialer.models import CallLog, Agent, Lead
from voice_orchestrator.redis import AGENT_STATE_LOCK_REDIS_KEY, LOCK_TIMEOUTS, SLEEP, SYNC_TO_DB_LOCK_REDIS_KEY, conn
from voice_orchestrator.utils import convert_wav_to_mp3, export_today_call_logs_to_csv, upload_call_logs, upload_call_recording
from .utils import add_active_call_in_cache, connect_agent_to_call, disconnect_call, fs_timestamp_to_datetime, is_agent_idle_in_cache, mark_agent_busy_in_cache, sync_to_db_wrapper, transfer_call, update_active_call_in_cache, transfer_agent_to_call, mark_agent_idle_in_cache, map_call_status, call_ending_routine, handle_free_agent, add_customer_to_waiting_queue, remove_customer_from_waiting_queue, get_next_customer_waiting_in_queue
from dialer.utils import get_next_available_secondary_sales_agent, remove_active_call, construct_queue_object, add_to_priority_queue_mapping, add_call_to_completed_list, get_and_clear_completed_calls, get_next_available_support_agent, get_next_available_sales_agent
from websocket.utils import push_agent_event

# ============================================================================
# MAIN EVENT PROCESSING ENTRY POINT
# ============================================================================

import time

logger = logging.getLogger(__name__)

REDIS_LOCK_KEY = "esl_listener_lock"
REDIS_LOCK_TTL = 10

# @shared_task(bind=True)
# def start_esl_listener(self):
#     lock = conn.lock(REDIS_LOCK_KEY, timeout=1)
#     if not lock.acquire(blocking=False):
#         logger.info("ESL listener already running. Exiting.")
#         return
    
#     while True:
#         try:
#             fs = InboundESL(
#                 host=settings.FREESWITCH_ESL_HOST,
#                 port=settings.FREESWITCH_ESL_PORT,
#                 password=settings.FREESWITCH_ESL_PASSWORD
#             )

#             fs.connect()

#             fs.send('event plain CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE CHANNEL_PARK CHANNEL_EXECUTE')

#             fs.register_handle('*', dispatch_event_handler)

#             logger.info("Freeswitch Connected. Listening for events...")

#             while fs.connected:
#                 fs.process_events()

#         except Exception as e:
#             logger.exception("ESL connection lost. Reconnecting in 1s...")
#             time.sleep(1)


def dispatch_event_handler(event) -> str:
    try:
        event_type = event.headers.get("Event-Name")
        direction = event.headers.get("Call-Direction")
        other_leg_uuid = event.headers.get("Other-Leg-Unique-ID", None)
        variable_uuid = event.headers.get("variable_uuid") #call_uuid
        variable_call_id = event.headers.get("variable_sip_h_X-call_id", None) #our internal call id to track in cache and db
        auto_bridge = event.headers.get("variable_sip_h_X-auto_bridge", None)
        agent_id = event.headers.get("variable_sip_h_X-agent_id", None)
        to_number = event.headers.get("variable_sip_h_X-to_number", None)
        logger.info(f"Received event ========>: {event_type}: direction={direction}, other_leg_uuid={other_leg_uuid}, variable_uuid={variable_uuid}, variable_call_id={variable_call_id}, agent_id={agent_id}, to_number={to_number}")
        if event_type == "CHANNEL_EXECUTE":
            app = event.headers.get("variable_current_application")  # or variable_current_application
            if app != "record_session":
                return  # ignore everything else

            recording_path = event.headers.get("variable_current_application_data")
            recording_path = '/'.join(recording_path.split('/')[-2:]) if recording_path else None
            if variable_call_id:
                update_active_call_in_cache(variable_call_id, {"recording_path": recording_path})
            else:
                add_active_call_in_cache(variable_uuid, {
                "agent_id": None,
                "payload": None,
                "call_uuid": variable_uuid,
                "recording_path": recording_path,
                "initiated_at": timezone.now().isoformat()
            })
            
        agent_lock = None
        if agent_id:
            lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
            agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
            if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
                pass
            else:
                logger.error(f"Could not acquire lock for agent {agent_id} - Event Handler - System Busy")
                return None
        try:
            if event_type == 'CHANNEL_ANSWER':
                if direction == 'outbound':
                    if not other_leg_uuid: #first answer
                        if not auto_bridge: #lead picked up first
                            if agent_id: #if agent is assigned to this lead, check if idle and connect. all leads except aquisition 
                                if is_agent_idle_in_cache(agent_id, check_call_id=True, check_state=False):
                                    connect_agent_to_call(agent_id, variable_uuid, variable_call_id)
                                else: #if not idle, disconnect call and add lead back to queue
                                    disconnect_call(variable_uuid, cause="USER_BUSY")
                            else: #aquisition calls with no agents
                                agent_id = get_next_available_sales_agent()
                                if not agent_id:
                                    agent_id = get_next_available_secondary_sales_agent()
                                if agent_id:
                                    connect_agent_to_call(agent_id, variable_uuid, variable_call_id)
                                else:
                                    disconnect_call(variable_uuid, cause="NO_AVAILABLE_AGENT")
                        else: #agent picked up first. priority or manual dial. 
                            if agent_id:
                                connect_agent_to_call(agent_id, variable_uuid, variable_call_id, to_number) #make the call to number  
                    else:
                        update_active_call_in_cache(variable_uuid, {"connected_at": time.time()})
                else:
                    # this case is for inbound pick ups. Since IVR is handled by freeswtich, we only connect the agent at park event 
                    # TODO This branch is also when a call is made from softphone dialer
                    # need to handle the case probably. 
                    pass

            # elif event_type == 'CHANNEL_EXECUTE': #TODO removing from register to reduce noise. to be later implemetned
            #     application = event.headers.get('Application')

            #     if application == 'transfer':
            #         uuid = event.headers.get('Unique-ID')
            #         # The new destination Agent B
            #         new_destination = event.headers.get('Application-Data') 
            #         # The agent who did the transferring
            #         transferor = event.headers.get('variable_last_sent_callee_id_number')
            #         # Try these in order to find Agent A
            #         # transferor = (
            #         #     event.headers.get('variable_last_sent_callee_id_number') or 
            #         #     event.headers.get('variable_caller_id_number') or 
            #         #     event.headers.get('variable_origination_caller_id_number')
            #         # )
            #         mark_agent_idle_in_cache(transferor) 
            #         mark_agent_busy_in_cache(new_destination)
            #         #TODO transfer commands probably in case softphone isn't handling. 

            # elif event_type == 'CHANNEL_PARK': #TODO removing park from register to reduce noise. to be later implemetned
            #     if direction == 'inbound':
            #         selection = event.headers.get('variable_ivr_choice')
            #         uuid = event.headers.get('Unique-ID')
            #         if selection == "1": #support
            #             agent_id = get_next_available_support_agent()
            #         elif selection == "2": #sales
            #             agent_id = get_next_available_secondary_sales_agent()
            #         else:
            #             logger.error('invalid ivr choice')
            #             return
            #         if agent_id:
            #             mark_agent_busy_in_cache(agent_id, uuid, variable_uuid)
            #             transfer_call(uuid, agent_id)
            #         else:
            #             team = 'support' if selection == '1' else 'secondary_sales'
            #             add_customer_to_waiting_queue(uuid, team)
            #             transfer_call(uuid, "waiting_room") #make sure this exists in freeswitch config 


            elif event_type == 'CHANNEL_ORIGINATE':
                # This event is used to track when the call is ringing. We can use this to update our cache and show ringing status in UI
                if direction == 'outbound' and agent_id:
                    push_agent_event(agent_id, 'ringing')


            elif event_type == 'CHANNEL_HANGUP_COMPLETE':
                if direction == 'inbound' and variable_call_id:
                    return #ignoring inbound leg if call made from backend. 

                hangup_cause = event.headers.get('variable_hangup_cause')
                logger.info(f"Call ended with hangup cause: {hangup_cause}")
                # if not variable_call_id:
                #     logger.error("No call_id found in event headers. Cannot process hangup complete event properly.")
                #     return


                call_details = remove_active_call(variable_call_id)
                if not call_details:
                    call_details = remove_active_call(variable_uuid) #for manual dials. 
                
                if not agent_id:
                    agent_id = call_details.get('agent_id', None) if call_details else None

                if hangup_cause in ['NO_AVAILABLE_AGENT', 'LOSE_RACE', 'USER_NOT_REGISTERED'] and call_details: #our internal call
                    if call_details and call_details.get('payload', None):
                        payload = call_details.get('payload')
                        add_lead_back_to_queue(payload.get('lead_id'))

                # if hangup_cause in ['BLIND_TRANSFER', 'ATTENDED_TRANSFER', 'TRANSFER']:
                #     transferor_ext = (
                #         event.headers.get('variable_user_name') or 
                #         event.headers.get('Caller-Caller-ID-Number') or 
                #         event.headers.get('variable_origination_caller_id_number')
                #     )
                #     # Mark the transferor agent as idle
                #     #TODO probably handled in transfer

                if agent_id:
                    push_agent_event(agent_id, 'call_ended', {"hangup_cause": hangup_cause})

                if hangup_cause == 'NORMAL_CLEARING':
                    if agent_id:
                        handle_free_agent(agent_id)
                call_ending_routine(call_details, event, direction)
            else:
                logger.debug(f"No specific handler for event type: {event_type}")
        except Exception as exc:
            logger.exception(f"Error processing event {event_type}: {exc}")
        finally:            
            if agent_id:
                if agent_lock:
                    if agent_lock.locked():
                        agent_lock.release()
            
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
            lead_id = payload.get('lead_id', None)
            # if not lead_id:
            #     logger.warning(f"No lead_id found in call payload for call_id {call_details.get('call_uuid', None)}. Skipping DB sync for this call.")
            #     continue

            disconnect_reason = call_details.get('disconnect_reason')
            initiated_at = call_details.get('initiated_at', None)
            connected_at = call_details.get('connected_at', None)
            ended_at = call_details.get('ended_at', None)
            duration_seconds = call_details.get('duration_seconds', None)
            direction = call_details.get('direction')

            agent_id = call_details.get('agent_id', None)
            phone_number = call_details.get('phone_number')
            call_status = map_call_status(disconnect_reason)
            call_uuid = call_details.get('call_uuid')

            connected_at = fs_timestamp_to_datetime(connected_at)
            ended_at = fs_timestamp_to_datetime(ended_at)
            recording_path = call_details.get('recording_path', None)
            billable_seconds = call_details.get('billable_seconds', None)

            call_log = CallLog.objects.create(
                call_id=call_uuid,
                agent_id=agent_id,
                lead_id=lead_id,
                to_number=phone_number,
                status=call_status,
                disconnect_reason=disconnect_reason,
                initiated_at=initiated_at,
                answered_at=connected_at,
                ended_at=ended_at,
                duration_seconds=duration_seconds,
                recording_url=recording_path,
                call_direction=direction,
                talk_time_seconds=billable_seconds
            )

            if call_status in ['answered', 'no_answer', 'busy', 'invalid'] and lead_id:
                if call_status == 'answered':
                    call_completed_lead_ids.append(lead_id)
                elif call_status in ['no_answer', 'busy']:
                    call_not_picked_lead_ids.append(lead_id)
                elif call_status == 'invalid':
                    invalid_lead_ids.append(lead_id)
            else:
                logger.warning(f"Call with call_id {call_details.get('call_uuid')} has unrecognized hangup cause: {call_details.get('disconnect_reason')}")
            
            logger.info(f"Call log created for call_id: {call_log.call_id}")

        if call_completed_lead_ids or call_not_picked_lead_ids or invalid_lead_ids:
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

@app.task
def upload_call_logs_to_s3():
    try:
        today = date.today()
        start_date = datetime.combine(today, datetime.min.time())
        end_date = datetime.combine(today, datetime.max.time())
        output_path = export_today_call_logs_to_csv(start_date, end_date)
        if not output_path:
            logger.error("Failed to export call logs to CSV. Aborting upload.")
            return
        url = upload_call_logs(output_path, today)
        logger.info(f"Call logs successfully uploaded to S3. Accessible at: {url}")
    except Exception as e:
        logger.exception(f"Error in upload_call_logs_to_s3: {e}")

@app.task
def daily_ending_routine():
    from dialer.utils import flush_redis_data, make_campaigns_inactive
    flush_redis_data()
    make_campaigns_inactive()
    logger.info("Daily call ending routine completed: Redis data flushed and campaigns marked inactive.")


@app.task
def upload_call_recording_to_s3(log_obj, recording_path):
    try:
        if isinstance(log_obj, int):
            log_obj = CallLog.objects.get(id=log_obj)

        mp3_path = convert_wav_to_mp3(recording_path)
        recording_date = (
            log_obj.initiated_at.date()
            if log_obj.initiated_at
            else timezone.now().date()
        )
        url = upload_call_recording(mp3_path, recording_date)
        log_obj.recording_url = url
        log_obj.save()
        logger.info(f"Call recording successfully uploaded to S3. Accessible at: {url}")
        return url
    except Exception as e:
        logger.exception(f"Error uploading call recording to S3: {e}")
        return None
