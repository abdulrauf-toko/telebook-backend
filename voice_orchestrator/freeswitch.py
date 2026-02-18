import greenswitch
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class FreeSwitchManager:
    def __init__(self):
        self._esl = None

    @property
    def esl(self):
        # Check if connection exists and is still alive
        if self._esl is None or not self._esl.connected:
            logger.info("Initializing new FreeSWITCH ESL connection...")
            self._esl = greenswitch.InboundESL(
                settings.FREESWITCH_ESL_HOST,
                settings.FREESWITCH_ESL_PORT,
                settings.FREESWITCH_ESL_PASSWORD
            )
            try:
                self._esl.connect()
            except Exception as e:
                logger.error(f"ESL Connection Failed: {e}")
                self._esl = None
        return self._esl

    def api(self, command):
        """Execute an API command and return data."""
        conn = self.esl
        if conn:
            try:
                response = conn.send(f"api {command}")
                return response.data
            except Exception as e:
                logger.error(f"Command execution failed: {e}")
                self._esl = None # Force reconnect on next attempt
        return None
    
    def bgapi(self, command):
        """Execute a non-blocking background API command."""
        conn = self.esl
        if conn:
            try:
                # bgapi returns a Job-UUID immediately
                response = conn.send(f"bgapi {command}")
                return response.data # Will look like "+OK Job-UUID: <uuid>"
            except Exception as e:
                logger.error(f"BGAPI failed: {e}")
                self._esl = None
        return None

# Global instance
fs_manager = FreeSwitchManager()