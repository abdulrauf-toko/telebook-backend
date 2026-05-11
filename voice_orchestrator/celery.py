"""
Celery app initialization for voice_orchestrator project.

Place this file at voice_orchestrator/__init__.py
Run this AFTER the existing __init__.py content (ensure it imports celery)

This ensures Celery starts when Django is imported.
"""

import os
from celery import Celery
from celery.schedules import crontab
from django.conf import settings

# Set default Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'voice_orchestrator.settings')

app = Celery('voice_orchestrator')

# Load configuration from Django settings with CELERY namespace
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all registered Django apps
app.autodiscover_tasks()

# Celery Beat schedule
PROD_BEAT_SCHEDULE = {
    'dialer-cycle-every-30-seconds': {
        'task': 'dialer.tasks.initiate_dialer_cycle',
        'schedule': 30.0,
    },
    'end-routine-daily': {
        'task': 'events.tasks.daily_ending_routine',
        'schedule': crontab(hour=19, minute=45),
    },
    'upload-call-recordings-daily': {
        'task': 'events.tasks.upload_days_call_recordings_to_s3_task',
        'schedule': crontab(hour=17, minute=0),
    },
    "day-start-routine": {
        'task': 'events.tasks.daily_start_routine',
        'schedule': crontab(hour=4, minute=5),
    }
}

app.conf.beat_schedule = PROD_BEAT_SCHEDULE if settings.ENV == 'PROD' else {}
