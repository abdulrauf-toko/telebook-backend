from collections import deque
from voice_orchestrator.redis import AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, COMPLETED_CALLS_REDIS_KEY, conn, LOCK_TIMEOUTS, SLEEP, \
      ACTIVE_CALLS_REDIS_KEY, AGENT_STATE_REDIS_KEY, AGENT_EXTENSION_MAPPING_REDIS_KEY, AGENT_LEAD_MAPPING_REDIS_KEY, \
      AGENT_STATE_LOCK_REDIS_KEY, ACTIVE_CALL_LOCK_REDIS_KEY, SYNC_TO_DB_LOCK_REDIS_KEY, AQUISITION_AGENTS_REDIS_KEY, \
      SUPPORT_CUSTOMERS_WAITING_QUEUE_REDIS_KEY, SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE_REDIS_KEY, DIALER_LOCK_KEY, AGENT_QUEUE_BY_GROUP_REDIS_KEY
import orjson as json
import logging
import time
from django.utils import timezone
from .models import Agent, Campaign, Lead
from typing import Dict, Optional
import uuid
from django.db.models import Case, When, IntegerField, Q
from voice_orchestrator.freeswitch import fs_manager
from voice_orchestrator.constants import TOKOLAB_NUMBER, QUEUE_REFILL_THRESHOLD, DEFAULT_PICKUP_RATIO, DIALER_EXECUTION_LOCK_TIMEOUT
from django.conf import settings
from django.contrib.auth.models import Group, User
from datetime import datetime
from collections import defaultdict
import os
logger = logging.getLogger(__name__)


def check_and_refill_queue():
    """
    Check queue levels and trigger refill tasks if needed.
    
    If queue length < QUEUE_REFILL_THRESHOLD:
    - Trigger async task to fetch more contacts
    - Replenish from database or external source
    """
    try:
        raw = conn.hgetall(AGENT_LEAD_MAPPING_REDIS_KEY)

        mapping_queue = {
            int(agent_id): json.loads(leads_json)
            for agent_id, leads_json in raw.items()
        }

        # all active/logged-in agents
        all_logged_in_agents = conn.hgetall(AGENT_STATE_REDIS_KEY)
        active_agents = {int(agent_id) for agent_id in all_logged_in_agents.keys()}

        agent_ids_to_refill = set()

        # CASE 1: no mapping exists at all
        if not mapping_queue:
            agent_ids_to_refill = active_agents

        else:
            # CASE 2: agents with low queue
            for agent_id in active_agents:
                leads = mapping_queue.get(agent_id)

                # missing mapping OR empty/low queue
                if not leads or len(leads) < QUEUE_REFILL_THRESHOLD:
                    agent_ids_to_refill.add(agent_id)

        logger.info(agent_ids_to_refill)

        if agent_ids_to_refill:
            refill_queue(agent_ids=agent_ids_to_refill)
        
    except Exception as exc:
        logger.exception(f"Error checking queues: {exc}")
        
def refill_queue(agent_ids: set):
    """
    Asynchronously refill priority queue.
    
    Fetches high-value/warm leads and populates priority queue.
    Non-blocking operation - runs in default queue.
    """
    from dialer.udhaar_utils import refill_emi_campaign_data
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
            .filter(
                active=True
            )
            .filter(Q(agent_id__in=agent_ids) | Q(agent_id__isnull=True))
            .annotate(segment_rank=segment_order)
            .order_by('segment_rank')
            .distinct()
        )

        logger.info(active_campaigns)

        new_queue = defaultdict(list)
        lead_ids = set()
        agent_ids_temp = set()  # Track agents for whom we're adding leads to the queue
        agents_to_fetch_leads = set()

        for active_campaign in active_campaigns:
            leads = active_campaign.leads.filter(status='pending').order_by('updated_at')
            count = 0
            agent_id = active_campaign.agent.id if active_campaign.agent else 0
            agent_id = str(agent_id)  # Redis keys must be strings

            logger.info(f"loop: {active_campaign}, {len(leads)}")
    
            if active_campaign.campaign_type == 'rupin_emi':
                if len(leads.filter(status='pending')) < 2:
                    agents_to_fetch_leads.add(agent_id)

            if agent_id != 0:
                agent = Agent.objects.get(id=int(agent_id))
                selected_campaign = agent.selected_campaign
                if selected_campaign and selected_campaign != active_campaign:
                    continue #only fill leads from selected campaign

            if agent_id in agent_ids_temp:
                continue 

            add = False
             #skip if we've already added leads for this agent in this refill cycle

            for lead in leads:
                add=True
                logger.info(f"lead: {lead}")
                lead_ids.add(lead.id)
                queue_object = construct_queue_object(active_campaign, lead)
                count += 1

                # acquisition → default queue
                # if active_campaign.segment == "acquisition":
                #     new_queue["0"].append(queue_object)
                #     if not added:
                #         add_agent_to_set(agent_id)
                #         added = True
                # else:
                #     new_queue[agent_id].append(queue_object)
                new_queue[agent_id].append(queue_object)

                if count >= 6:
                    break  # Limit to 6 leads per campaign to avoid outdated leads. 

            if add:
                agent_ids_temp.add(agent_id)

        logger.info(f'AGENTS {str(agents_to_fetch_leads)}')
        if agents_to_fetch_leads:
            refill_emi_campaign_data(agents_to_fetch_leads)

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


def validate_and_cleanup_agent_states():
    """
    Validates all agent states and handles orphaned calls.
    
    For each busy agent:
    1. If current_call_id exists, check if it's in active calls. If not, mark agent idle.
    2. If current_call_id is None and call_initiated_at is > 90 seconds ago, mark agent idle.
    
    This prevents agents from being stuck in "busy" state when their calls are orphaned.
    """
    from events.utils import handle_free_agent, remove_agent_from_cache, log_agent_authentication_action
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
                registered = is_user_registered(agent_id)
                if not registered and agent_data.get('state') == 'idle':
                    remove_agent_from_cache(agent_id)
                    log_agent_authentication_action(int(agent_id), 'logout')
                    logger.info(f"Removed unregistered agent {agent_id} from cache")
                    continue #TODO send event to client to logout if agent is unregistered.

                
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
                    # elif call_initiated_at and current_time - call_initiated_at > AVERAGE_CALL_DURATION * 2: #if call has been active for more than 3 times the average duration, consider it orphaned
                    #     logger.warning(
                    #         f"Agent {agent_id} has a long-running call {current_call_id} "
                    #         f"(initiated_at: {call_initiated_at}). Marking agent idle."
                    #     )
                    #     handle_free_agent(agent_id)
                    #     cleanup_count += 1

                
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

def process_priority_queue() -> int:
    """
    Process priority queue with 1:1 agent-to-contact assignment.
    """
    from events.utils import is_agent_idle_in_cache
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
    from events.utils import is_agent_idle_in_cache
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
    from events.utils import is_agent_idle_in_cache

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

            logger.info(f"Aquisition: {total_calls_dialed} calls dialed")
            return total_calls_dialed
        
    except Exception as exc:
        logger.exception(f"Error processing acquisition queue: {exc}")
        return total_calls_dialed
    finally:
        if queue_lock.owned():
            queue_lock.release()

def get_priority_queue_mapping():
    raw_data = conn.hgetall(AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY)
    if not raw_data:
        return {}

    try:
        return {k: json.loads(v) for k, v in raw_data.items()}
    except json.JSONDecodeError:
        logger.error("Failed to decode PRIORITY_NUMBERS_TO_CALL_REDIS_KEY")
        return {}
    
def add_to_priority_queue_mapping(agent_id, entry):
    if not entry:
        raise Exception('payload empty')
    
    lock_key = f"{AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY}:lock"
    queue_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    
    try:
        if queue_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            queue_mapping = get_priority_queue_mapping()
            agent_id_str = str(agent_id)
            
            if agent_id_str in queue_mapping:
                queue_mapping[agent_id_str].append(entry)
            else:
                queue_mapping[agent_id_str] = [entry]
            
            # Store using HSET
            for aid, queue in queue_mapping.items():
                conn.hset(AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, aid, json.dumps(queue))
            
        else:
            logger.error("Could not acquire lock for priority queue")
    except Exception as e:
        logger.error(f"Error adding to priority queue: {e}")
    finally:
        if queue_lock.owned():
            queue_lock.release()

def construct_agent_queue_redis_key(group_name):
    return f"{AGENT_QUEUE_BY_GROUP_REDIS_KEY}:{group_name}"

def get_all_idle_agents():
    group_names = list(Group.objects.values_list('name', flat=True))
    free_agent_count = 0
    for name in group_names:
        redis_key = construct_agent_queue_redis_key(name)
        free_agent_count += conn.zrange(redis_key, 0, -1)
    
    return free_agent_count

def get_all_idle_agents_by_group(group_name):
    redis_key = construct_agent_queue_redis_key(group_name)
    return conn.zrange(redis_key, 0, -1)

def get_all_active_calls():
    calls = conn.hgetall(ACTIVE_CALLS_REDIS_KEY)
    return calls

def remove_active_call(call_id):
    if not call_id:
        logger.error("No call_id provided to remove_active_call")
        return None
    pipe = conn.pipeline()
    pipe.hget(ACTIVE_CALLS_REDIS_KEY, call_id)
    pipe.hdel(ACTIVE_CALLS_REDIS_KEY, call_id)
    results = pipe.execute()
    raw_data = results[0]
    
    if raw_data:
        return json.loads(raw_data)
    return None

def add_call_to_completed_list(call_details):
    try:
        serialized_details = json.dumps(call_details)
        conn.rpush(COMPLETED_CALLS_REDIS_KEY, serialized_details)
        return True

    except Exception as e:
        logger.error(f"Error adding completed call to cache: {e}")
        return False
    
def get_and_clear_completed_calls():
    """
    Retrieves all completed calls from the Redis list and clears the list atomically.
    Uses a lock to prevent race conditions.

    Returns:
        list: A list of dictionaries representing completed calls.
              Returns an empty list if the key does not exist or is empty.
    """
    lock_key = f"{COMPLETED_CALLS_REDIS_KEY}:lock"
    call_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if call_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            with conn.pipeline() as pipe:
                pipe.lrange(COMPLETED_CALLS_REDIS_KEY, 0, -1)
                pipe.delete(COMPLETED_CALLS_REDIS_KEY)
                serialized_calls, _ = pipe.execute()

            # Deserialize the calls
            completed_calls = [json.loads(call) for call in serialized_calls]
            return completed_calls

        else:
            logger.error("Could not acquire lock for completed calls - System Busy")
            return []

    except Exception as e:
        logger.error(f"Error retrieving/clearing completed calls: {e}")
        return []

    finally:
        if call_lock.owned():
            call_lock.release()


def add_agent_to_group_queue(agent_id):
    agent = Agent.objects.select_related('user').get(id=agent_id)
    group = agent.user.groups.first()
    if not group:
        return
    redis_key = construct_agent_queue_redis_key(group.name)
    conn.zadd(redis_key, {agent_id: time.time()})

def remove_agent_from_group_queue(agent_id):
    try:
        agent = Agent.objects.select_related('user').get(id=agent_id)
        group = agent.user.groups.first()
        if not group:
            return False
        redis_key = construct_agent_queue_redis_key(group.name)
        conn.zrem(redis_key, agent_id)
        return True
    except Exception as e:
        logger.error(f"Error removing agent {agent_id} from queue: {e}")
        return False

def get_agent_state(agent_id):
    state = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
    return json.loads(state) if state else {}

def get_all_agent_states():
    return conn.get(AGENT_STATE_REDIS_KEY)       

def get_next_available_agent(group_name):
    try:
        redis_key = construct_agent_queue_redis_key(group_name)
        result = conn.zpopmin(redis_key, count=1)
        if not result:
            return None
        agent_id, _ = result[0]
        return agent_id
    except Exception as exc:
        logger.exception(f"Error finding next available agent for group {group_name}: {exc}")
        return None
    
def peek_next_available_agent(group_name):
    try:
        redis_key = construct_agent_queue_redis_key(group_name)
        result = conn.zrange(redis_key, 0, 0)
        if not result:
            return None
        return result[0]
    except Exception as exc:
        logger.exception(f"Error peeking next available agent for group {group_name}: {exc}")
        return None   
    

def construct_queue_object(campaign, lead):
    return {
        "campaign_id": campaign.id if campaign else None,
        "campaign_name": campaign.campaign_name if campaign else None,
        "campaign_segment": campaign.segment if campaign else None,

        "lead_id": lead.id,
        "udhaar_lead_id": lead.udhaar_lead_id,

        "phone_number": lead.phone_number,
        "customer_name": lead.customer_name,
        "city": lead.city,

        "month_gmv": lead.month_gmv,
        "overall_gmv": lead.overall_gmv,

        "total_orders": lead.total_orders,
        "address": lead.address,

        
        "last_call_date": (
            lead.last_call_date.isoformat()
            if lead.last_call_date else None
        ),
        "last_order_details": lead.last_order_details or {},
        "follow_up_date": lead.follow_up_date,
        "follow_up_time": lead.follow_up_time,
        "comment": lead.comment,
        
        "metadata": lead.metadata or {},
        "enqueued_at": timezone.now().isoformat(),
    }
    
def add_to_agent_queue_mapping(agent_id, details): #TODO
    conn.sadd(AGENT_LEAD_MAPPING_REDIS_KEY, agent_id)

def get_agent_extension_mapping_from_cache():
    raw = conn.get(AGENT_EXTENSION_MAPPING_REDIS_KEY)
    if raw:
        return json.loads(raw)
    else:
        return get_agent_extension_mapping()

def get_agent_extension_mapping():
    all_agents = Agent.objects.all()
    mapping = {}
    for agent in all_agents:
        mapping[str(agent.id)] = agent.extension

    return mapping

def get_agent_extension(agent_id):
    mapping = get_agent_extension_mapping_from_cache()
    return mapping[str(agent_id)]

def make_outbound_call_helper(agent_id, leads, calls_to_make=1):
    from events.utils import add_active_call_in_cache, mark_agent_busy_in_cache, is_agent_idle_in_cache

    calls_dialed = 0
    removed = False

    auto_bridge = calls_to_make == 1
    # park = calls_to_make > 1

    if agent_id == '0' and auto_bridge:
        agent_id = peek_next_available_agent('sales')
        if not agent_id:
            logger.error("No available agents for priority call")
            return calls_dialed, leads
        
        if not is_agent_idle_in_cache(agent_id):
            remove_agent_from_group_queue(agent_id)
            return calls_dialed, leads

    lead_queue = deque(leads)
    
    for call_number in range(min(calls_to_make, len(leads))):
        lead = lead_queue[0] 
        
        phone_number = lead.get('phone_number')
        
        payload = lead.copy()
        payload.pop('last_order_details', None)
        payload.pop('metadata', None)
        
        if not phone_number:
            lead_queue.popleft() #remove lead from queue if no phone number to avoid retrying
            logger.warning(f"Contact {lead} missing phone number")
            continue
        
        success, call_uuid = originate_call(
            agent_id=agent_id,
            phone_number=phone_number,
            payload=payload,
            auto_bridge=auto_bridge
        )

        if success:
            if not removed:
                success = remove_agent_from_group_queue(agent_id)
                if success:
                    if auto_bridge:
                        success = mark_agent_busy_in_cache(agent_id, call_uuid) #mark busy with call id
                    else:
                        success = mark_agent_busy_in_cache(agent_id, None) #mark busy without call id for predictive dialer logic

                    if not success:
                        add_agent_to_group_queue(agent_id)

                if not success:
                    return calls_dialed, list(lead_queue) #if we fail to remove agent from queue, we should not proceed with more calls to avoid duplicates
                removed = True

            add_active_call_in_cache(call_uuid, {
                "agent_id": agent_id if agent_id != "0" else None,
                "phone_number": phone_number,
                "payload": payload,
                "call_uuid": call_uuid,
                "initiated_at": timezone.now().isoformat()
            })
            calls_dialed += 1
            lead_queue.popleft()
        else:
            logger.error(f"Failed to originate call to {phone_number} for agent {agent_id}")

    return calls_dialed, list(lead_queue)


def make_outbound_call_helper_aquisition(agent_id, leads, calls_to_make=1):
    from events.utils import add_active_call_in_cache, mark_agent_busy_in_cache
    
    calls_dialed = 0
    removed = False

    lead_queue = deque(leads)
    
    for call_number in range(min(calls_to_make, len(leads))):
        lead = lead_queue[0] 
        
        phone_number = lead.get('phone_number')
        
        payload = lead.copy()
        payload.pop('last_order_details', None)
        payload.pop('metadata', None)
        
        if not phone_number:
            lead_queue.popleft() #remove lead from queue if no phone number to avoid retrying
            logger.warning(f"Contact {lead} missing phone number")
            continue
        
        success, call_uuid = originate_call(
            agent_id=None, #decide after pick up
            phone_number=phone_number,
            payload=payload,
            auto_bridge=False
        )

        if success:
            if not removed:
                success = mark_agent_busy_in_cache(agent_id, None, remove_from_queue=False) #mark busy without call id for predictive dialer logic, remove form queue after deciding at pickup

                if not success:
                    return calls_dialed, list(lead_queue) #if we fail to mark agent busy, we should not proceed with more calls
                removed = True

            add_active_call_in_cache(call_uuid, {
                "agent_id": None,
                "phone_number": phone_number,
                "payload": payload,
                "call_uuid": call_uuid,
                "initiated_at": timezone.now().isoformat()
            })
            calls_dialed += 1
            lead_queue.popleft()
        else:
            logger.error(f"Failed to originate call to {phone_number} for agent {agent_id}")

    return calls_dialed, list(lead_queue)


def originate_call(
    phone_number: str,
    agent_id: Optional[str] = None,
    payload: Optional[dict] = None,
    auto_bridge: bool = False
):

    try:
        call_uuid = str(uuid.uuid4())      
        
        originate_command = build_originate_command(
            call_id=call_uuid,
            phone_number=phone_number,
            agent_id=agent_id,
            payload=payload,
            auto_bridge=auto_bridge
        )
        
        if not originate_command:
            raise Exception("originate command failed")
        
        job_response = fs_manager.bgapi(originate_command)

        if job_response and "+OK" in job_response:
            job_uuid = job_response.split(" ")[-1]
            
            logger.info(f"Call initiated. Tracking Job: {job_uuid}")
            return True, call_uuid
        else:
            logger.error(f"Failed to start call: {job_response}")
            return False, None
    
    except Exception as exc:
        logger.exception(f"Error originating call to {phone_number}: {exc}")
        return False


def build_originate_command(
    call_id: str,
    phone_number: str,
    agent_id: Optional[str] = None,
    payload: Optional[dict] = None,
    auto_bridge: bool = False,
) -> str:
    try:
        # Build metadata
        payload['call_id'] = call_id
        payload['origination_uuid'] = call_id
        
        
        if agent_id: 
            payload["agent_id"] = agent_id

        # if park:
            # application = 'custom_park' 
        # else:
            # application = "&bridge"

        application = 'custom_park'

        if auto_bridge:
            payload['auto_bridge'] = "true"
            payload['to_number'] = phone_number

        var_string = ','.join([f"sip_h_X-{k}='{v}'" for k, v in payload.items()])

        if settings.ENV == 'PROD':
            extension = get_agent_extension(agent_id) if agent_id else None
            if auto_bridge:
                call_string = f"sofia/internal/{extension}@{settings.SIP_IP}"
            else:
                call_string = f"sofia/gateway/tokolab_trunk/{phone_number}"
                # call_string = f"sofia/gateway/tokolab_trunk/03152526525"
        else:
            call_string = f"user/{phone_number}" 

        originate_cmd = (
            f"originate {{origination_caller_id_number={TOKOLAB_NUMBER},"
            f"{var_string}}}{call_string} {application}"
        )
        
        return originate_cmd
        
    except Exception as exc:
        logger.exception(f"Error building originate command: {exc}")
        return None

def is_user_registered(id: str) -> bool:
    extension = get_agent_extension(id)
    result = fs_manager.api("show registrations")
    logger.info(f"Checking registration for extension {extension}: {result}")
    if not result:
        return False
    for line in result.split("\n"):
        if line.startswith(f"{extension},"):
            return True
    return False


def get_recording_path(call_uuid: str):
    date = datetime.now().strftime("%Y-%m-%d")
    directory = f"/home/pbx/telebook-pbx/recordings/{date}"
    os.makedirs(directory, exist_ok=True)
    return f"{directory}/{call_uuid}.wav"


def get_disposition_mapping(disconnect_reason: str):
    from events.utils import FS_TO_DJANGO_STATUS
    return FS_TO_DJANGO_STATUS.get(disconnect_reason, 'unknown')


def flush_redis_data():
    try:
        keys_to_delete = [
            AGENT_STATE_REDIS_KEY,
            AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY,
            AGENT_LEAD_MAPPING_REDIS_KEY,
            ACTIVE_CALLS_REDIS_KEY,
            COMPLETED_CALLS_REDIS_KEY,
            AGENT_EXTENSION_MAPPING_REDIS_KEY,
            SYNC_TO_DB_LOCK_REDIS_KEY,
            AQUISITION_AGENTS_REDIS_KEY,
            SUPPORT_CUSTOMERS_WAITING_QUEUE_REDIS_KEY,
            SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE_REDIS_KEY,
        ]
        key_prefixes_to_delete = [
            AGENT_STATE_LOCK_REDIS_KEY,
            ACTIVE_CALL_LOCK_REDIS_KEY,
        ]

        deleted_count = conn.delete(*keys_to_delete)

        for key_prefix in key_prefixes_to_delete:
            for key in conn.scan_iter(f"{key_prefix}*"):
                deleted_count += conn.delete(key)

        logger.info("Redis dialer data cleared successfully. Deleted %s keys", deleted_count)
    except Exception as e:
        logger.error(f"Error flushing Redis data: {e}")

def make_campaigns_inactive():
    campaigns = Campaign.objects.filter(active=True)
    count = campaigns.update(active=False)
    logger.info(f"Marked {count} campaigns as inactive")


def active_campaigns(agent):
    try:
        
        campaigns = Campaign.objects.filter(
            agent=agent,
            active=True,
        ).order_by('-created_at')

        response_list = []

        for campaign in campaigns:
            data = {
                'id': campaign.id,
                'campaign_id': campaign.campaign_id,
                'segment': campaign.segment,
                'count': campaign.leads.count()
            }
            if agent.selected_campaign == campaign:
                data['active'] = True
            else:
                data['active'] = False
            response_list.append(data)
        
        return response_list
    except Exception as e:
        logger.exception(f"Error fetching active campaigns: {e}")

def reset_selected_campaign():
    Agent.objects.all().update(selected_campaign=None)


def create_emi_campaigns():
    emi_agents = Agent.objects.filter(teams__name='rupin_emi')
    today = timezone.now().strftime("%Y%m%d")

    for agent in emi_agents:
        agent_username = agent.telecard_username
        campaign_id = "{}-emi-{}".format(agent_username, today)
        _, created = Campaign.objects.get_or_create(
            campaign_id=campaign_id,
            defaults={
                "agent": agent,
                "segment": "other",
                "campaign_type": "rupin_emi",
                "campaign_name": "{} - EMI".format(agent_username),
                "active": True,
            },
        )
        logger.info("create_emi_campaigns: {} campaign {} for agent {}".format(
            "created" if created else "found existing", campaign_id, agent_username
        ))