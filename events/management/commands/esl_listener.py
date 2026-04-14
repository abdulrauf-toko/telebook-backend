# management/commands/esl_listener.py
from django.core.management.base import BaseCommand
from events.tasks import dispatch_event_handler
from django.conf import settings
from greenswitch import InboundESL
import logging
import time

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Run the FreeSWITCH ESL listener'

    def handle(self, *args, **options):
        while True:
            try:
                fs = InboundESL(
                    host=settings.FREESWITCH_ESL_HOST,
                    port=settings.FREESWITCH_ESL_PORT,
                    password=settings.FREESWITCH_ESL_PASSWORD
                )
                fs.connect()
                fs.send('event plain CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE CHANNEL_PARK CHANNEL_EXECUTE CHANNEL_PROGRESS CHANNEL_ORIGINATE CHANNEL_CREATE')
                fs.register_handle('*', dispatch_event_handler)
                logger.info("FreeSWITCH Connected. Listening for events...")
                while fs.connected:
                    fs.process_events()
            except Exception as e:
                logger.exception("ESL connection lost. Reconnecting in 1s...")
                time.sleep(1)