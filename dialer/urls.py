"""
URL configuration for Dialer API endpoints.

Simple function-based views for campaigns, leads, and CSV uploads.
"""

from django.urls import path
from . import views

urlpatterns = [
    path('agent/login/', views.agent_login, name='agent-login'),
    path('agent/logout/', views.logout_agent_api, name='agent-logout'),
    path('campaigns/active/', views.active_campaigns, name='active-campaigns'),
    path('dashboard/', views.agent_dashboard, name='agent-dashboard'),
    path('manager-dashboard/', views.manager_dashboard, name='manager-dashboard'),
    path('all-call-logs/', views.all_call_logs_dashboard, name='all-call-logs-dashboard'),
    path('call-logs-recordings/', views.call_logs_recordings_dashboard, name='call-logs-recordings-dashboard'),
    path('call-logs-recordings/exports/start/', views.start_call_logs_recording_export, name='start-call-logs-recording-export'),
    path('call-logs-recordings/exports/<int:export_id>/status/', views.call_logs_recording_export_status, name='call-logs-recording-export-status'),
    path('call-logs-recordings/downloads/', views.call_logs_recording_downloads, name='call-logs-recording-downloads'),
    path('manual-call-log/', views.manual_call_log, name='manual-call-log'),
    path('form-submission/', views.formdata_submission, name='form-submission'),
    
    # Call management endpoints
    path('call/', views.initiate_call, name='initiate-call'),
    path('call/<str:uuid>/', views.poll_call_status, name='poll-call-status'),
    path('call-recordings/<str:uuid>/', views.get_call_recording, name='get-call-recording'),
    
    # Campaign endpoints
    path('agent/campaign/activate/', views.activate_campaign, name='activate-campaign'),
    path('agent/campaign/deactivate/', views.deactivate_campaign, name='deactivate-campaign'),
]
