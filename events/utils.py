import orjson as json
from events.tasks import sync_to_db
from voice_orchestrator.redis import ACTIVE_CALL_LOCK_REDIS_KEY, SYNC_TO_DB_LOCK_REDIS_KEY, conn, AGENT_STATE_REDIS_KEY, SALES_AGENT_QUEUE_REDIS_KEY, SUPPORT_AGENT_QUEUE_REDIS_KEY, AGENT_STATE_LOCK_REDIS_KEY, SLEEP, LOCK_TIMEOUTS, ACTIVE_CALLS_REDIS_KEY
import logging
import time
from voice_orchestrator.freeswitch import fs_manager
from dialer.utils import add_call_to_completed_list, add_sales_agent_to_queue, get_agent_team, add_support_agent_to_queue, get_pending_support_agent, get_pending_sales_agent, get_next_available_support_agent, get_next_available_sales_agent, add_to_priority_queue_mapping, get_agent_extension

logger = logging.getLogger(__name__)

FS_TO_DJANGO_STATUS = {
    # Answered
    'NORMAL_CLEARING': 'answered',
    
    # Busy
    'USER_BUSY': 'busy',
    'CALL_REJECTED': 'busy', # Usually means they hit "Decline"
    
    # No Answer
    'NO_ANSWER': 'no_answer',
    'NO_USER_RESPONSE': 'no_answer',
    'PROGRESS_TIMEOUT': 'no_answer',
    
    # Failed / System Issues
    'RECOVERY_ON_TIMER': 'failed',
    'ORIGINATOR_CANCEL': 'cancelled',
    'LOSE_RACE': 'failed',
    
    # Invalid Number
    'UNALLOCATED_NUMBER': 'invalid',
    'INVALID_NUMBER_FORMAT': 'invalid',
    'NO_ROUTE_DESTINATION': 'invalid',
}


def mark_agent_logged_in_cache(agent_id, team):
    lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
    
    # timeout=5: If the process crashes, the lock dies in 3 seconds
    # sleep=0.05: If locked, wait 50ms before trying again
    agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
            if not raw_data: #AGENT logged out before call end event
                agent_data = {"state": "idle", "current_call_id": None, "team": team}
            else:
                agent_data = json.loads(raw_data)
                agent_data.update({"state": "idle", "current_call_id": None})
            
            if agent_data.get('team') == 'sales':
                queue_key = SALES_AGENT_QUEUE_REDIS_KEY
            else:
                queue_key = SUPPORT_AGENT_QUEUE_REDIS_KEY


            #Pipe ensures both updates is done via a single round-trip. Better for performance
            with conn.pipeline() as pipe:
                pipe.hset(AGENT_STATE_REDIS_KEY, agent_id, json.dumps(agent_data))
                pipe.zadd(queue_key, {agent_id: time.time()}) #scored set. guarantees deduplication, and removes the the agent in front of the queue
                pipe.execute()
            return agent_data

        else:
            logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
            return None
            
    except Exception as e:
        logger.error(f"Error updating agent {agent_id}: {e}")
        return None
    finally:
        if agent_lock.owned():
            agent_lock.release()

def mark_agent_idle_in_cache(agent_id): #changes both state and adds to queue.
    lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
    
    # timeout=5: If the process crashes, the lock dies in 3 seconds
    # sleep=0.05: If locked, wait 50ms before trying again
    agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
            if not raw_data: #AGENT logged out before call end event
                return None
            
            agent_data = json.loads(raw_data)
            if agent_data.get('team') == 'sales':
                queue_key = SALES_AGENT_QUEUE_REDIS_KEY
            else:
                queue_key = SUPPORT_AGENT_QUEUE_REDIS_KEY
            agent_data.update({"state": "idle", "current_call_id": None})

            #Pipe ensures both updates is done via a single round-trip. Better for performance
            with conn.pipeline() as pipe:
                pipe.hset(AGENT_STATE_REDIS_KEY, agent_id, json.dumps(agent_data))
                pipe.zadd(queue_key, {agent_id: time.time()}) #scored set. guarantees deduplication, and removes the the agent in front of the queue
                pipe.execute()
            return agent_data

        else:
            logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
            return None
            
    except Exception as e:
        logger.error(f"Error updating agent {agent_id}: {e}")
        return None
    finally:
        if agent_lock.owned():
            agent_lock.release()

def mark_agent_busy_in_cache(agent_id, call_id):
    lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
    agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
            if not raw_data:
                return None #AGENT LOGGED OUT WHILE BRIDGING
            
            agent_data = json.loads(raw_data)
            # Update agent state
            agent_data.update({
                "state": "busy",
                "current_call_id": call_id
            })

            if agent_data.get('team') == 'sales':
                queue_key = SALES_AGENT_QUEUE_REDIS_KEY
            else:
                queue_key = SUPPORT_AGENT_QUEUE_REDIS_KEY

            with conn.pipeline() as pipe:
                pipe.hset(AGENT_STATE_REDIS_KEY, agent_id, json.dumps(agent_data))
                pipe.zrem(queue_key, agent_id)  # remove from idle queue
                pipe.execute()

            return True
        else:
            logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
            return None

    except Exception as e:
        logger.error(f"Error marking agent {agent_id} busy: {e}")
        return None

    finally:
        if agent_lock.owned():
            agent_lock.release()


def is_agent_idle_in_cache(agent_id, check_call_id=False, check_state=True):
    lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
    agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
            if not raw_data:
                return False  # Agent not found or logged out

            agent_data = json.loads(raw_data)
            agent_state = agent_data.get("state")
            current_call_id = agent_data.get("current_call_id")

            if check_state:
                if agent_state != "idle":
                    return False
            if check_call_id:
                if current_call_id is not None:
                    return False
            return True
        else:
            logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
            return False

    except Exception as e:
        logger.error(f"Error checking idle state for agent {agent_id}: {e}")
        return False

    finally:
        if agent_lock.owned():
            agent_lock.release()

def get_all_idle_agents_in_cache(check_call_id=False, check_state=True):
    try:
        all_agents = conn.hgetall(AGENT_STATE_REDIS_KEY)
        idle_agents = []
        for agent_id, raw_data in all_agents.items():
            agent_data = json.loads(raw_data)
            agent_state = agent_data.get("state")
            current_call_id = agent_data.get("current_call_id")

            if check_state:
                if agent_state != "idle":
                    continue
            if check_call_id:
                if current_call_id is not None:
                    continue
            idle_agents.append(agent_id)
            
        return idle_agents
    except Exception as e:
        logger.error(f"Error getting all idle agents: {e}")
        return []       

def remove_agent_from_cache(agent_id):
    lock_key = f"{AGENT_STATE_LOCK_REDIS_KEY}{agent_id}"
    agent_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if agent_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(AGENT_STATE_REDIS_KEY, agent_id)
            if not raw_data:
                return None #AGENT LOGGED OUT 
            
            agent_data = json.loads(raw_data)
            if agent_data.get('team') == 'sales':
                queue_key = SALES_AGENT_QUEUE_REDIS_KEY
            else:
                queue_key = SUPPORT_AGENT_QUEUE_REDIS_KEY

            with conn.pipeline() as pipe:
                pipe.hdel(AGENT_STATE_REDIS_KEY, agent_id)
                pipe.zrem(queue_key, agent_id)  # remove from idle queue if present
                pipe.execute()
            return True
        else:
            logger.error(f"Could not acquire lock for agent {agent_id} - System Busy")
            return None
    except Exception as e:
        logger.error(f"Error removing agent {agent_id} from cache: {e}")
        return None
    finally:
        if agent_lock.owned():
            agent_lock.release()
    


    
def add_active_call_in_cache(call_id, details):
    conn.hset(ACTIVE_CALLS_REDIS_KEY, call_id, json.dumps(details))

def update_active_call_in_cache(call_id, details):
    lock_key = f"{ACTIVE_CALL_LOCK_REDIS_KEY}{call_id}"
    call_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)

    try:
        if call_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            raw_data = conn.hget(ACTIVE_CALLS_REDIS_KEY, call_id)
            if not raw_data:
                return False  # Call not found

            existing_details = json.loads(raw_data)
            updated_details = {**existing_details, **details}

            with conn.pipeline() as pipe:
                pipe.hset(ACTIVE_CALLS_REDIS_KEY, call_id, json.dumps(updated_details))
                pipe.execute()

            return True
        else:
            logger.error(f"Could not acquire lock for call {call_id} - System Busy")
            return False

    except Exception as e:
        logger.error(f"Error updating active call {call_id}: {e}")
        return False

    finally:
        if call_lock.owned():
            call_lock.release()


def remove_active_call_in_cache(call_id):
    # Create a pipeline for atomic-like behavior
    pipe = conn.pipeline()
    # pipe.hget(ACTIVE_CALLS_REDIS_KEY, call_id)
    # pipe.hdel(ACTIVE_CALLS_REDIS_KEY, call_id)

    # results[0] is the hget output, results[1] is the hdel count
    results = pipe.execute()
    raw_data = results[0]

    if raw_data:
        return json.loads(raw_data)
    return None


def bridge_call(agent_id, event_obj):
    try:
        # Extract call information from the event
        call_uuid = event_obj.channel_uuid
        
        if not call_uuid:
            logger.error(f"No channel UUID found in event for agent {agent_id}")
            return None
        
        success = bridge_agent_to_call(call_uuid, agent_id)
        if not success:
            raise Exception(f'get_body didnt return with +OK') #TODO simplify/remove this in prod
        
        # Mark the agent as busy with this call
        agent_busy_result = mark_agent_busy_in_cache(agent_id, call_uuid)
        
        if not agent_busy_result:
            logger.error(f"Failed to mark agent {agent_id} as busy for call {call_uuid}")
            return None
        
        # Prepare call details to store in active calls cache
        call_details = {
            "agent_id": agent_id,
            "channel_uuid": call_uuid,
            "caller_id_number": event_obj.caller_id_number,
            "caller_id_name": event_obj.caller_id_name,
            "destination_number": event_obj.destination_number,
            "channel_name": event_obj.channel_name,
            "bridged_at": time.time(),
            "event_id": str(event_obj.event_id)
        }
        
        # Add the call to active calls cache
        add_active_call_in_cache(call_uuid, call_details)
        
        logger.info(f"Successfully bridged agent {agent_id} to call {call_uuid}")
        
        return {
            "status": "bridged",
            "agent_id": agent_id,
            "call_id": call_uuid,
            "call_details": call_details
        }
        
    except Exception as e:
        logger.error(f"Error bridging agent {agent_id} to call: {e}")
        return None


def bridge_agent_to_call(call_uuid, agent_id):
    extension = get_agent_extension(agent_id)
    agent_destination = f"user/{extension}"
    result = fs_manager.api(f"uuid_bridge {call_uuid} {agent_destination}")
    if result.getBody().startswith("+OK"):
        logger.info(f"Successfully bridging {call_uuid} to Agent extension: {extension}")
        return True
    return False

def connect_agent_to_call(agent_id, call_uuid):
    success = bridge_agent_to_call(call_uuid, agent_id)
    if success:
        mark_agent_busy_in_cache(agent_id, call_uuid)
        update_active_call_in_cache(call_uuid, {"agent_id": agent_id, "connected_at": time.time()}) 
    else:
        logger.error(f"Failed to bridge call {call_uuid} to agent {agent_id}")
        disconnect_call(call_uuid, cause="LOSE_RACE")

def disconnect_call(call_uuid, cause="NORMAL_CLEARING"):
    cmd = f"uuid_kill {call_uuid} {cause}"
    result = fs_manager.api(cmd)
    
    if result.getBody().startswith("+OK"):
        logger.info(f"Call {call_uuid} has been disconnected.")
        return True
    else:
        logger.error(f"Failed to disconnect: {result.getBody()}")
        return False
    
def transfer_call(call_uuid, agent_id):
    extension = get_agent_extension(agent_id)
    dialplan = "XML"
    context = "default"
    command = f"uuid_transfer {call_uuid} {extension} {dialplan} {context}"
    return fs_manager.bgapi(command)
    

def handle_no_available_sales_agents(event_obj):
    result = disconnect_call(call_uuid=event_obj.channel_uuid, cause="LOSE_RACE")
    if not result:
        pass #TODO handle fallback logic
    add_to_priority_queue_mapping(event_obj.caller_id_number)
    return {"status": "disconnected", 'message': "no agents free"} 


def handle_free_agent(agent_id):
    mark_agent_idle_in_cache(agent_id)
    # agent_team = get_agent_team(agent_id)
    # if agent_team == 'support':
    #     add_support_agent_to_queue(agent_id)
    # elif agent_team == "sales":
    #     add_sales_agent_to_queue(agent_id)
    # elif agent_team == 'secondary_sales':
    #     add_sales_agent_to_queue(agent_id) #TODO change this. 


def map_call_status(hangup_cause):
    return FS_TO_DJANGO_STATUS.get(hangup_cause)

def sync_to_db_wrapper():
    if conn.get(SYNC_TO_DB_LOCK_REDIS_KEY):
        logger.info("Sync to DB already in progress by another worker. Skipping this run.")
        return False
    conn.set(SYNC_TO_DB_LOCK_REDIS_KEY, "locked", ex=5)
    sync_to_db.apply_async(countdown=5)

def call_ending_routine(call_details, event, direction):
    if call_details:
        call_details["ended_at"] = event.getHeader("Caller-Channel-Hangup-Time")
        call_details["disconnect_reason"] = event.getHeader("Hangup-Cause")
        call_details["duration_seconds"] = int(event.getHeader("variable_duration"))
        call_details['direction'] = direction
        add_call_to_completed_list(call_details)
        sync_to_db_wrapper()