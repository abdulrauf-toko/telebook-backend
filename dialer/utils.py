from collections import deque
from voice_orchestrator.redis import AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, COMPLETED_CALLS_REDIS_KEY, conn, LOCK_TIMEOUTS, SLEEP, SALES_AGENT_QUEUE_REDIS_KEY, \
      SUPPORT_AGENT_QUEUE_REDIS_KEY, ACTIVE_CALLS_REDIS_KEY, AGENT_STATE_REDIS_KEY, AGENT_EXTENSION_MAPPING_REDIS_KEY, AGENT_LEAD_MAPPING_REDIS_KEY, \
      SECONDARY_SALES_AGENT_QUEUE_REDIS_KEY
import orjson as json
import logging
import time
from django.utils import timezone
from .models import Agent
# from events.utils import add_active_call_in_cache, mark_agent_busy_in_cache, is_agent_idle_in_cache
from typing import Dict, List, Tuple, Optional
import uuid
from voice_orchestrator.freeswitch import fs_manager
from voice_orchestrator.constants import ORIGINATE_TIMEOUT
from django.conf import settings

logger = logging.getLogger(__name__)

def get_priority_queue_mapping():
    raw_data = conn.get(AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY)
    if not raw_data:
        return {}

    try:
        return json.loads(raw_data)  # returns list of dicts
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
            if agent_id in queue_mapping:
                queue_mapping[str(agent_id)].append(entry)
            else:
                queue_mapping[str(agent_id)] = [entry]
                
            conn.set(AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY, json.dumps(queue_mapping))
            
        else:
            logger.error("Could not acquire lock for priority queue")
    except Exception as e:
        logger.error(f"Error adding to priority queue: {e}")
    finally:
        if queue_lock.owned():
            queue_lock.release()


def get_all_idle_agents():
    support = conn.zrange(SUPPORT_AGENT_QUEUE_REDIS_KEY, 0, -1)
    sales = conn.zrange(SALES_AGENT_QUEUE_REDIS_KEY, 0, -1)
    
    return support + sales

def get_all_idle_support_agents():
    return conn.zrange(SUPPORT_AGENT_QUEUE_REDIS_KEY, 0, -1)

def get_all_idle_sales_agents():
    return conn.zrange(SALES_AGENT_QUEUE_REDIS_KEY, 0, -1)

def get_all_active_calls():
    sales = conn.hgetall(ACTIVE_CALLS_REDIS_KEY)

    return sales

def get_all_active_sales_calls():
    # return conn.hgetall(SALES_ACTIVE_CALLS_REDIS_KEY)
    pass

def remove_active_call(call_id):
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


def add_sales_agent_to_queue(agent_id):
    conn.zadd(SALES_AGENT_QUEUE_REDIS_KEY, {agent_id: time.time()})

def add_support_agent_to_queue(agent_id):
    conn.zadd(SUPPORT_AGENT_QUEUE_REDIS_KEY, {agent_id: time.time()})

def add_secondary_sales_agent_to_queue(agent_id):
    conn.zadd(SECONDARY_SALES_AGENT_QUEUE_REDIS_KEY, {agent_id: time.time()})

def remove_agent_from_sales_queue(agent_id):
    try:
        conn.zrem(SALES_AGENT_QUEUE_REDIS_KEY, agent_id)
        return True
    except Exception as e:
        logger.error(f"Error removing agent {agent_id} from queue: {e}")
        return False
    
def remove_agent_from_support_queue(agent_id):
    try:
        conn.zrem(SUPPORT_AGENT_QUEUE_REDIS_KEY, agent_id)
        return True
    except Exception as e:
        logger.error(f"Error removing agent {agent_id} from queue: {e}")
        return False

def get_agent_state(agent_id):
    return conn.hget(AGENT_STATE_REDIS_KEY, agent_id)

def get_all_agent_states():
    return conn.get(AGENT_STATE_REDIS_KEY)

def get_agent_team(agent_id):
    agent = get_agent_state(agent_id)
    return agent.get('agent_team')

def get_pending_support_agent():
    all_agents = get_all_agent_states()
    for agent, details in all_agents.items():
        if details.get('state') == 'pending' and details.get('agent_team') == "support":
            return agent
        
def get_pending_sales_agent():
    all_agents = get_all_agent_states()
    for agent, details in all_agents.items():
        if details.get('state') == 'pending' and details.get('agent_team') == "sales":
            return agent
        

def get_next_available_sales_agent():
    try:
        result = conn.zpopmin(SALES_AGENT_QUEUE_REDIS_KEY, count=1)
        if not result:
            return None

        agent_id, _ = result[0]
        return agent_id 
        
    except Exception as exc:
        logger.exception(f"Error finding next available sales agent: {exc}")
        return None
    
def peek_next_available_sales_agent():
    try:
        # Peek the lowest-scored member without removing it
        result = conn.zrange(SALES_AGENT_QUEUE_REDIS_KEY, 0, 0)
        if not result:
            return None

        return result[0]
        
    except Exception as exc:
        logger.exception(f"Error peeking next available sales agent: {exc}")
        return None
    
def get_next_available_support_agent():
    try:
        result = conn.zpopmin(SUPPORT_AGENT_QUEUE_REDIS_KEY, count=1)
        if not result:
            return None

        agent_id, _ = result[0]
        return agent_id 
        
    except Exception as exc:
        logger.exception(f"Error finding next available support agent: {exc}")
        return None
    

def get_next_available_secondary_sales_agent():
    try:
        result = conn.zpopmin(SECONDARY_SALES_AGENT_QUEUE_REDIS_KEY, count=1)
        if not result:
            return None

        agent_id, _ = result[0]
        return agent_id 
        
    except Exception as exc:
        logger.exception(f"Error finding next available support agent: {exc}")
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

        "customer_segment": lead.customer_segment,
        "month_gmv": lead.month_gmv,
        "overall_gmv": lead.overall_gmv,

        
        "last_call_date": (
            lead.last_call_date.isoformat()
            if lead.last_call_date else None
        ),
        "last_order_details": lead.last_order_details or {},
        
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
    park = calls_to_make > 1

    if agent_id == '0' and auto_bridge:
        agent_id = peek_next_available_sales_agent()
        if not agent_id:
            logger.error("No available agents for priority call")
            return calls_dialed, leads
        
        if not is_agent_idle_in_cache(agent_id):
            remove_agent_from_sales_queue(agent_id)
            return calls_dialed, leads
    else:
        logger.error('agent_id cannot be 0 without auto bridge')

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
            park=park,
            auto_bridge=auto_bridge
        )

        if success:
            if not removed:
                success = remove_agent_from_sales_queue(agent_id)
                if success:
                    if auto_bridge:
                        success = mark_agent_busy_in_cache(agent_id, call_uuid) #mark busy without call id for predictive dialer logic
                    else:
                        success = mark_agent_busy_in_cache(agent_id, None) #mark busy without call id for predictive dialer logic

                    if not success:
                        add_sales_agent_to_queue(agent_id)

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
            park=True,
            auto_bridge=False
        )

        if success:
            if not removed:
                success = mark_agent_busy_in_cache(agent_id, None) #mark busy without call id for predictive dialer logic

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


# ============================================================================
# CALL ORIGINATION - FREESWITCH INTEGRATION
# ============================================================================

def originate_call(
    phone_number: str,
    park: bool,
    agent_id: Optional[str] = None,
    payload: Optional[dict] = None,
    auto_bridge: bool = False
) -> Dict:

    try:
        call_uuid = str(uuid.uuid4())      
        
        originate_command = build_originate_command(
            call_id=call_uuid,
            phone_number=phone_number,
            agent_id=agent_id,
            payload=payload,
            auto_bridge=auto_bridge,
            park=park
        )
        
        logger.info(f"Originate command: {originate_command}")
        if not originate_command:
            raise Exception("originate command failed")
        
        job_response = fs_manager.bgapi(originate_command)

        if job_response and "+OK" in job_response:
            job_uuid = job_response.split(" ")[-1]
            
            logger.info(f"Call initiated. Tracking Job: {job_uuid}")
            return True, call_uuid
        else:
            logger.error("Failed to start call")
            return False, None
    
    except Exception as exc:
        logger.exception(f"Error originating call to {phone_number}: {exc}")
        return False


def build_originate_command(
    call_id: str,
    phone_number: str,
    park: bool,
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

        if park:
            application = '&park' 
            payload['originate_timeout'] = ORIGINATE_TIMEOUT #disconnect after x seconds if fails to bridge
        else:
            application = "&bridge"

        bridge_to_string = ""
        if auto_bridge:
            agent_extension = get_agent_extension(agent_id)
            bridge_to_string = f"(user/{agent_extension})"
            payload['auto_bridge'] = "true"

        var_string = ','.join([f"sip_h_X-{k}='{v}'" for k, v in payload.items()])

        if settings.ENV == 'PROD':
            call_string = f"sofia/external/{phone_number}"
        else:
            call_string = f"user/{phone_number}" #phone number is extension (eg 1000)

        
        originate_cmd = (
            f"originate {{{var_string}}}{call_string} {application}{bridge_to_string}"
        )
        
        return originate_cmd
        
    except Exception as exc:
        logger.exception(f"Error building originate command: {exc}")
        return None
