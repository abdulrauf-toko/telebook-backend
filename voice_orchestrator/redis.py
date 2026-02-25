import redis
from django.conf import settings
from redis import ConnectionPool

##### NAMESPACES
AGENT_STATE_REDIS_KEY = 'AGENT_STATES' #dict where key is agent id, value is object with details (call_id, status, etc) 
SUPPORT_AGENT_QUEUE_REDIS_KEY = "SUPPORT_AGENT_QUEUE" #sorted set where each element is agent id, and sorted by time 
SALES_AGENT_QUEUE_REDIS_KEY = "SALES_AGENT_QUEUE" #sorted set where each element is agent id, and sorted by time
SECONDARY_SALES_AGENT_QUEUE_REDIS_KEY = "SECONDARY_SALES_AGENT_QUEUE" #sorted set where each element is agent id, and sorted by time
AGENT_PRIORITY_LEAD_MAPPING_REDIS_KEY = "AGENT_PRIORITY_LEAD_MAPPING" #dict where key is agent id, value is list of lead objects  
AGENT_LEAD_MAPPING_REDIS_KEY = "AGENT_LEAD_MAPPING" #dict where key is agent id, value is list of lead objects  
ACTIVE_CALLS_REDIS_KEY = "ACTIVE_CALLS" #dict where call_id is key, and object is call details (agent_id, started, etc)
# SUPPORT_ACTIVE_CALLS_REDIS_KEY = "SUPPORT_ACTIVE_CALLS"
COMPLETED_CALLS_REDIS_KEY = "COMPLETED_CALLS"
AGENT_STATE_LOCK_REDIS_KEY = "AGENT_STATE_LOCK:"  
ACTIVE_CALL_LOCK_REDIS_KEY = "ACTIVE_CALL_LOCK:"
AGENT_EXTENSION_MAPPING_REDIS_KEY = "AGENT_EXTENSION_MAPPING"
SYNC_TO_DB_LOCK_REDIS_KEY = "SYNC_TO_DB_LOCK"
AQUISITION_AGENTS_REDIS_KEY = "AQUISITION_AGENTS" #sorted set where each element is agent id, and sorted by time
SUPPORT_CUSTOMERS_WAITING_QUEUE_REDIS_KEY = "SUPPORT_CUSTOMERS_WAITING_QUEUE" #list where each element is call_id of customer waiting in queue
SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE_REDIS_KEY = "SECONDARY_SALES_CUSTOMERS_WAITING_QUEUE"

LOCK_TIMEOUTS = 3
SLEEP = 0.05
#####


# pool = ConnectionPool(
#     host=settings.CELERY_BROKER_URL,
#     db=settings.CELERY_RESULT_BACKEND,
#     decode_responses=True
#     )

# conn = redis.Redis(connection_pool=pool)
conn = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True
)