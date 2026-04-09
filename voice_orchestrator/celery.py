"""
Celery app initialization for voice_orchestrator project.

Place this file at voice_orchestrator/__init__.py
Run this AFTER the existing __init__.py content (ensure it imports celery)

This ensures Celery starts when Django is imported.
"""

import os
from celery import Celery

# Set default Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'voice_orchestrator.settings')

app = Celery('voice_orchestrator')

# Load configuration from Django settings with CELERY namespace
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all registered Django apps
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
