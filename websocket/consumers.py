# myapp/consumers.py
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from dialer.models import Agent

logger = logging.getLogger(__name__)

class AgentConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.agent_id = self.scope['url_route']['kwargs']['agent_id']
        user = await get_user(self.agent_id)

        await self.channel_layer.group_add(f"agent_{self.agent_id}", self.channel_name)
        # Register into group
        await self.channel_layer.group_add(f"groups_{user.groups.first().name}", self.channel_name)
        await self.accept()
        logger.info(f"Agent {self.agent_id} connected")

    async def disconnect(self, close_code):
        # Remove from group on disconnect
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info(f"Agent {self.agent_id} disconnected: {close_code}")

    # Called when message is pushed to group
    async def agent_event(self, event):
        await self.send(text_data=json.dumps(event))

from asgiref.sync import sync_to_async

@sync_to_async
def get_user(agent_id):
    return User.objects.prefetch_related('groups').get(agent__id=agent_id)
