
import orjson as json
import logging
from voice_orchestrator.redis import AGENT_STATE_LOCK_REDIS_KEY, conn, AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, AGENT_LEAD_MAPPING_REDIS_KEY, LOCK_TIMEOUTS, SLEEP, AQUISITION_AGENTS_REDIS_KEY, ACTIVE_CALLS_REDIS_KEY, AGENT_STATE_REDIS_KEY
from django.utils import timezone
from voice_orchestrator.celery import app
import time
from .utils import get_priority_queue_mapping, construct_queue_object, make_outbound_call_helper, make_outbound_call_helper_aquisition
from collections import defaultdict
from .models import Lead, Campaign
from django.db.models import Case, When, IntegerField
from voice_orchestrator.constants import DEFAULT_PICKUP_RATIO, AVERAGE_CALL_DURATION, AGENT_FREE_PREDICTION_WINDOW, QUEUE_REFILL_THRESHOLD, DIALER_EXECUTION_LOCK_TIMEOUT, PERIODIC_TRIGGER_INTERVAL, PREDICTIVE_DIALING
from events.utils import is_agent_idle_in_cache, get_all_idle_agents_in_cache, handle_free_agent
from .udhaar_utils import store_campaigns_from_df
import requests
import pandas as pd
from io import StringIO
from django.conf import settings
from websocket.utils import push_agent_event

logger = logging.getLogger(__name__)

DIALER_LOCK_KEY = 'dialer:execution_lock'
AGENT_CALL_COUNT_KEY = 'agent:call_count:{agent_id}'
AGENT_STATE_CACHE_KEY = 'agent_state:{agent_id}'
DIALER_METRICS_KEY = 'dialer:metrics:latest'

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
            logger.info("No available agents, skipping cycle")
            return {
                'status': 'skipped',
                'reason': 'no_agents_available',
                'timestamp': cycle_start.isoformat()
            }
        
        logger.info(f"Effective agent capacity: {agent_capacity}")
        
        # Step 2: Process priority queue
        priority_dialed = process_priority_queue()
        agent_capacity -= priority_dialed
        
        # logger.info(f"Priority queue processed: {priority_dialed} calls") 
        
        # Step 3: Process secondary queue (predictive dialing)
        secondary_dialed = process_secondary_queue()
        logger.info(f"Secondary queue processed: {secondary_dialed} calls")
        
        # aquisition_dialed = process_aquisition_queue()
        # logger.info(f"Aquisition queue processed: {aquisition_dialed} calls")

        # Step 4: Check and refill queues if needed
        check_and_refill_queue()
        
        cycle_duration = (timezone.now() - cycle_start).total_seconds()
        total_calls_dialed = secondary_dialed
        
        metrics = {
            'status': 'completed',
            'timestamp': cycle_start.isoformat(),
            'duration_seconds': cycle_duration,
            'agent_capacity': agent_capacity,
            # 'priority_calls_dialed': priority_dialed,
            'secondary_calls_dialed': secondary_dialed,
            'total_calls_dialed': total_calls_dialed
        }
        
        logger.info(f"=== DIALER CYCLE COMPLETE === {total_calls_dialed} calls dialed")
        return metrics
            
    except Exception as exc:
        logger.exception(f"Error in dialer cycle: {exc}")
        
        return {
            'status': 'error',
            'error': str(exc),
            'timestamp': timezone.now().isoformat()
        }
    
    finally:
        release_dialer_lock()


# ============================================================================
# DIALER LOCK MANAGEMENT
# ============================================================================

def acquire_dialer_lock():
    try:
        lock = conn.set(DIALER_LOCK_KEY, '1', ex=DIALER_EXECUTION_LOCK_TIMEOUT, nx=True)
        return lock is not None
    except Exception as e:
        logger.error(f"Error acquiring dialer lock: {e}")
        return False

def release_dialer_lock():
    try:
        conn.delete(DIALER_LOCK_KEY)
        return True
    except Exception as e:
        logger.error(f"Error releasing dialer lock: {e}")
        return False

# ============================================================================
# ALGORITHM IMPLEMENTATION - STEP 2: PROCESS PRIORITY QUEUE
# ============================================================================

def process_priority_queue() -> int:
    """
    Process priority queue with 1:1 agent-to-contact assignment.
    """

    total_calls_dialed = 0
    lock_key = f"{AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY}:lock"
    queue_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    
    try:
        
        if queue_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
        
            # Get priority queue
            priority_queue_mapping = get_priority_queue_mapping()

            if not priority_queue_mapping:
                logger.info("Priority queue is empty")
                return 0

            for agent_id, leads in priority_queue_mapping.items():
                if not leads: #list empty
                    continue 
                
                lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
                agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

                try:
                    if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):

                        if not is_agent_idle_in_cache(agent_id=agent_id, check_call_id=True, check_state=True):
                            logger.info(f"Agent {agent_id} is not idle, skipping")
                            continue
                        
                        # Originate call
                        calls_dialed, leads_left = make_outbound_call_helper(
                            agent_id=agent_id,
                            leads=leads,
                            calls_to_make=1 #one to one mapping for priority queue
                        )

                        total_calls_dialed += calls_dialed
                        priority_queue_mapping[agent_id] = leads_left
                    
                    else:
                        logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
                        return False
            
                except Exception as e:
                    logger.error(f"Error checking idle state for agent {agent_id}: {e}")
                    return False
            
                finally:
                    if agent_lock.owned():
                        agent_lock.release()

            # Update priority queue in cache using HSET
            conn.hset(AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, mapping={
                agent_id: json.dumps(leads_list) 
                for agent_id, leads_list in priority_queue_mapping.items()
            })

            logger.info(f"Priority queue: {total_calls_dialed} calls dialed")
            return total_calls_dialed
    
        
    except Exception as exc:
        logger.exception(f"Error processing priority queue: {exc}")
        return total_calls_dialed
    finally:
        if queue_lock.owned():
            queue_lock.release()



# ============================================================================
# ALGORITHM IMPLEMENTATION - STEP 3: PROCESS SECONDARY QUEUE
# ============================================================================

def process_secondary_queue() -> int:
    total_calls_dialed = 0
    lock_key = f"{AGENT_LEAD_MAPPING_REDIS_KEY}:lock"
    queue_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    try:
        if queue_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            # Get secondary queue
            raw = conn.hgetall(AGENT_LEAD_MAPPING_REDIS_KEY)
            secondary_queue = {
                agent_id: json.loads(leads_json)
                for agent_id, leads_json in raw.items()
            }

            if not secondary_queue:
                logger.info("Secondary queue is empty")
                return 0

            # logger.info(f'secondary_queue: {secondary_queue}')
            # # Get pickup ratio (can be per-campaign or global)
            pickup_ratio = DEFAULT_PICKUP_RATIO
            dial_multiplier = max(1, int(1 / pickup_ratio))  # floor(1/y)

            logger.info(f"Pickup ratio: {pickup_ratio}, Dial multiplier: {dial_multiplier}") 

            # # Calculate calls to initiate
            # calls_to_initiate = available_capacity * dial_multiplier
            # calls_to_initiate = min(calls_to_initiate, len(secondary_queue))

            # logger.info(f"Initiating {calls_to_initiate} predictive dials")

            for agent_id, leads in secondary_queue.items():
                if not leads:
                    continue

                if agent_id == '0':
                    continue #handle acquisition leads later on

                lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
                agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

                try:
                    if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):

                        if not is_agent_idle_in_cache(agent_id, check_call_id=True, check_state=True):
                            logger.info(f"no available agent found for agent_id {agent_id}, skipping predictive dial")
                            continue
                        
                        calls_dialed, leads_left = make_outbound_call_helper(agent_id, leads, calls_to_make=dial_multiplier)
                        total_calls_dialed += calls_dialed
                        secondary_queue[agent_id] = leads_left
                    
                    else:
                        logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
                        return False
            
                except Exception as e:
                    logger.error(f"Error checking idle state for agent {agent_id}: {e}")
                    return False
            
                finally:
                    if agent_lock.owned():
                        agent_lock.release()
                
            data_to_store = {
                agent_id: json.dumps(leads_list) 
                for agent_id, leads_list in secondary_queue.items()
            }

            # Update secondary queue in cache
            conn.hset(AGENT_LEAD_MAPPING_REDIS_KEY, mapping=data_to_store)

            logger.info(f"Secondary queue: {total_calls_dialed} calls dialed")
            return total_calls_dialed
        
    except Exception as exc:
        logger.exception(f"Error processing secondary queue: {exc}")
        return total_calls_dialed
    finally:
        if queue_lock.owned():
            queue_lock.release()


def process_aquisition_queue() -> int:

    aquisition_agents = get_aquisition_set()
    
    total_calls_dialed = 0
    lock_key = f"{AGENT_LEAD_MAPPING_REDIS_KEY}:lock"
    queue_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    try:
        if queue_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            # Get secondary queue
            raw = conn.hgetall(AGENT_LEAD_MAPPING_REDIS_KEY)
            secondary_queue = {
                agent_id: json.loads(leads_json)
                for agent_id, leads_json in raw.items()
            }

            if not secondary_queue:
                logger.info("Secondary queue is empty")
                return 0

            logger.info(f'secondary_queue: {secondary_queue}')
            # # Get pickup ratio (can be per-campaign or global)
            pickup_ratio = DEFAULT_PICKUP_RATIO
            dial_multiplier = max(1, int(1 / pickup_ratio))  # floor(1/y)

            logger.info(f"Pickup ratio: {pickup_ratio}, Dial multiplier: {dial_multiplier}") 

            # # Calculate calls to initiate
            # calls_to_initiate = len(available_agents) * dial_multiplier

            # logger.info(f"Initiating {calls_to_initiate} predictive dials")
            leads = secondary_queue.get("0", [])
            if not leads:
                logger.info("No leads in acquisition queue")
                return 0
                
            for agent_id in aquisition_agents:
                lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
                agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

                try:
                    if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):

                        if not is_agent_idle_in_cache(agent_id, check_call_id=True, check_state=True):
                            logger.info(f"no available agent found for agent_id {agent_id}, skipping acquisition dial")
                            continue
                    
                        calls_dialed, leads = make_outbound_call_helper_aquisition(agent_id, leads, calls_to_make=dial_multiplier)
                        total_calls_dialed += calls_dialed
                    
                    else:
                        logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
                        return False
                except Exception as e:
                    logger.error(f"Error checking idle state for agent {agent_id}: {e}")
                    return False
                finally:                    
                    if agent_lock.owned():
                        agent_lock.release()
                
            secondary_queue["0"] = leads

            # Update secondary queue in cache
            conn.hset(AGENT_LEAD_MAPPING_REDIS_KEY, "0", json.dumps(leads))

            logger.info(f"Secondary queue: {total_calls_dialed} calls dialed")
            return calls_dialed
        
    except Exception as exc:
        logger.exception(f"Error processing secondary queue: {exc}")
        return calls_dialed
    finally:
        if queue_lock.owned():
            queue_lock.release()

# ============================================================================
# QUEUE MANAGEMENT
# ============================================================================

@app.task(bind=True)
def check_and_refill_queue(self):
    """
    Check queue levels and trigger refill tasks if needed.
    
    If queue length < QUEUE_REFILL_THRESHOLD (default 100):
    - Trigger async task to fetch more contacts
    - Replenish from database or external source
    """
    try:
        raw = conn.hgetall(AGENT_LEAD_MAPPING_REDIS_KEY)
        mapping_queue = {
            agent_id: json.loads(leads_json)
            for agent_id, leads_json in raw.items()
        }
        
        if not mapping_queue:
            refill_queue()
        else:
            for agent_id, leads in mapping_queue.items():
                if len(leads) < QUEUE_REFILL_THRESHOLD:
                    refill_queue.apply_async()
        
    except Exception as exc:
        logger.exception(f"Error checking queues: {exc}")
        
@app.task(bind=True)
def refill_queue(self):
    """
    Asynchronously refill priority queue.
    
    Fetches high-value/warm leads and populates priority queue.
    Non-blocking operation - runs in default queue.
    """
    try:
        segment_order = Case(
            When(segment='follow_up', then=0),
            When(segment='active', then=1),
            When(segment='growth', then=2),
            When(segment='active_churn', then=3),
            When(segment='growth_churn', then=4),
            When(segment='acquisition', then=5),
            When(segment='other', then=6),
            default=7,
            output_field=IntegerField(),
        )
        active_campaigns = (
            Campaign.objects
            .filter(active=True, leads__status='pending')
            .annotate(segment_rank=segment_order)
            .order_by('segment_rank')
            .distinct()
        )

        new_queue = defaultdict(list)
        lead_ids = set()

        for active_campaign in active_campaigns:
            added = False
            campaign_leads = []
            leads = active_campaign.leads.filter(status='pending')
            for lead in leads:
                lead_ids.add(lead.id)
                queue_object = construct_queue_object(active_campaign, lead)
                campaign_leads.append(queue_object)

                agent_id = active_campaign.agent.id if active_campaign.agent else 0
                agent_id = str(agent_id)  # Redis keys must be strings

                # acquisition → default queue
                if active_campaign.segment == "acquisition":
                    new_queue["0"].append(queue_object)
                    if not added:
                        add_agent_to_set(agent_id)
                        added = True
                else:
                    new_queue[agent_id].append(queue_object)

        updated = Lead.objects.filter(
            id__in=lead_ids,
            status='pending'
        ).update(
            status='in_queue',
            updated_at=timezone.now()
        )

        if not updated:
            return
        
        lock_key = f"{AGENT_LEAD_MAPPING_REDIS_KEY}:lock"
        queue_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
        try:
            if queue_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):

                raw = conn.hgetall(AGENT_LEAD_MAPPING_REDIS_KEY)
                queue_mapping = {
                    agent_id: json.loads(leads_json)
                    for agent_id, leads_json in raw.items()
                }
                
                for agent, leads in new_queue.items():
                    agent_key = str(agent)  # Redis-safe keys
                    if agent_key not in queue_mapping:
                        queue_mapping[agent_key] = []

                    queue_mapping[agent_key].extend(leads)

                data_to_store = {
                    agent_id: json.dumps(leads_list) 
                    for agent_id, leads_list in queue_mapping.items()
                }

                # Update secondary queue in cache
                conn.hset(AGENT_LEAD_MAPPING_REDIS_KEY, mapping=data_to_store)

                logger.info(f"Queue refilled with {len(lead_ids)} contacts")
        except Exception as e:
            logger.exception(f"Error updating queue in redis: {e}")
        finally:
            if queue_lock.owned():
                queue_lock.release()

    except Exception as exc:
        logger.exception(f"Error refilling priority queue: {exc}")


def add_agent_to_set(agent_id):
    
    lock_key = f"{AQUISITION_AGENTS_REDIS_KEY}:lock"
    agent_list_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    
    try:
        if agent_list_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            # Get existing set or create empty one
            raw_data = conn.get(AQUISITION_AGENTS_REDIS_KEY)
            if raw_data:
                agent_set = set(json.loads(raw_data))
            else:
                agent_set = set()
            
            # Add agent_id to set (automatically handles duplicates)
            agent_set.add(agent_id)
            
            # Store updated set with 8 hour expiry
            conn.set(AQUISITION_AGENTS_REDIS_KEY, json.dumps(list(agent_set)), ex=28800)
            return True
        else:
            logger.error(f"Could not acquire lock for {AQUISITION_AGENTS_REDIS_KEY}")
            return False
            
    except Exception as e:
        logger.error(f"Error adding agent to set: {e}")
        return False
    finally:
        if agent_list_lock.owned():
            agent_list_lock.release()


def get_aquisition_set():
    try:
        raw_data = conn.get(AQUISITION_AGENTS_REDIS_KEY)
        if not raw_data:
            return set()
        
        return set(json.loads(raw_data))
    except json.JSONDecodeError:
        logger.error(f"Failed to decode set at key {AQUISITION_AGENTS_REDIS_KEY}")
        return set()
    except Exception as e:
        logger.error(f"Error retrieving agent set: {e}")
        return set()


# ============================================================================
# AGENT VALIDATION AND CLEANUP
# ============================================================================

def validate_and_cleanup_agent_states():
    """
    Validates all agent states and handles orphaned calls.
    
    For each busy agent:
    1. If current_call_id exists, check if it's in active calls. If not, mark agent idle.
    2. If current_call_id is None and call_initiated_at is > 90 seconds ago, mark agent idle.
    
    This prevents agents from being stuck in "busy" state when their calls are orphaned.
    """
    
    try:
        # Get all agent states
        all_agent_states = conn.hgetall(AGENT_STATE_REDIS_KEY)
        if not all_agent_states:
            logger.info("No agent states to validate")
            return
        
        active_calls = conn.hgetall(ACTIVE_CALLS_REDIS_KEY)
        current_time = time.time()
        cleanup_count = 0
        
        for agent_id, raw_agent_data in all_agent_states.items():
            try:
                agent_data = json.loads(raw_agent_data)
                
                # Skip if agent is not busy
                if agent_data.get('state') != 'busy':
                    continue
                
                current_call_id = agent_data.get('current_call_id')
                call_initiated_at = agent_data.get('call_initiated_at')
                
                # Case 1: Agent has a call_id but it doesn't exist in active calls
                if current_call_id:
                    if current_call_id not in active_calls:
                        logger.warning(
                            f"Agent {agent_id} has orphaned call {current_call_id}. "
                            f"Marking agent idle."
                        )
                        handle_free_agent(agent_id)
                        cleanup_count += 1
                    elif call_initiated_at and current_time - call_initiated_at > AVERAGE_CALL_DURATION * 2: #if call has been active for more than 3 times the average duration, consider it orphaned
                        logger.warning(
                            f"Agent {agent_id} has a long-running call {current_call_id} "
                            f"(initiated_at: {call_initiated_at}). Marking agent idle."
                        )
                        handle_free_agent(agent_id)
                        cleanup_count += 1

                
                # Case 2: Agent has no call_id but initiated_at is over 90 seconds ago
                elif call_initiated_at is not None and current_time - call_initiated_at > 90:
                    logger.warning(
                        f"Agent {agent_id} is busy with no call for > 90s "
                        f"(initiated_at: {call_initiated_at}). Marking agent idle."
                    )
                    handle_free_agent(agent_id)
                    cleanup_count += 1
                    
            except json.JSONDecodeError:
                logger.error(f"Failed to decode agent state for {agent_id}")
                continue
            except Exception as e:
                logger.error(f"Error validating agent {agent_id}: {e}")
                continue
        
        if cleanup_count > 0:
            logger.info(f"Cleaned up {cleanup_count} orphaned agent states")
        else:
            logger.debug("No orphaned agent states found")
            
    except Exception as e:
        logger.exception(f"Error in validate_and_cleanup_agent_states: {e}")

@app.task(bind=True)
def fetch_and_store_telebook_campaign(self):
    MAX_TRIES = 10
    POLL_INTERVAL = 30  # seconds
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
                process_telebook_csv.delay(df.to_json())  # pass to next task
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


@app.task
def process_telebook_csv(df_json):
    df = pd.read_json(df_json)
    store_campaigns_from_df(df)

