# myapp/utils/ws.py
import logging
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)

def push_agent_event(agent_id, event: str, data: dict = None):
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"agent_{agent_id}",
            {
                "type": "agent.event",
                "event": event,
                "data": data or {},
            }
        )
    except Exception as e:
        logger.error(f"Failed to push event to agent {agent_id}: {e}")