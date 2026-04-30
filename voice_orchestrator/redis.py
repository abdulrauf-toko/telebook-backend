import redis
from django.conf import settings

##### NAMESPACES
AGENT_STATE_REDIS_KEY = 'AGENT_STATES' #dict where key is agent id, value is object with details (call_id, status, etc) 
AGENT_QUEUE_BY_GROUP_REDIS_KEY = "AGENT_QUEUE:" #each agent group will maintain it's own redis key, where it's a sorted set where each element is agent id
AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY = "AGENT_PRIORITY_LEAD_MAPPING" #dict where key is agent id, value is list of lead objects  
AGENT_LEAD_MAPPING_REDIS_KEY = "AGENT_LEAD_MAPPING" #dict where key is agent id, value is list of lead objects  
ACTIVE_CALLS_REDIS_KEY = "ACTIVE_CALLS" #dict where call_id is key, and object is call details (agent_id, started, etc)
COMPLETED_CALLS_REDIS_KEY = "COMPLETED_CALLS" #list of call objects. 
AGENT_STATE_LOCK_REDIS_KEY = "AGENT_STATE_LOCK:" 
ACTIVE_CALL_LOCK_REDIS_KEY = "ACTIVE_CALL_LOCK:"
AGENT_EXTENSION_MAPPING_REDIS_KEY = "AGENT_EXTENSION_MAPPING"
SYNC_TO_DB_LOCK_REDIS_KEY = "SYNC_TO_DB_LOCK"
AQUISITION_AGENTS_REDIS_KEY = "AQUISITION_AGENTS" #sorted set where each element is agent id, and sorted by time
SUPPORT_CUSTOMERS_WAITING_QUEUE_REDIS_KEY = "SUPPORT_CUSTOMERS_WAITING_QUEUE" #list where each element is call_id of customer waiting in queue
SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE_REDIS_KEY = "SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE"
DIALER_LOCK_KEY = 'DIALER_LOCK'

LOCK_TIMEOUTS = 3
SLEEP = 0.05

conn = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True
)