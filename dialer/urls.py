"""
URL configuration for Dialer API endpoints.

Simple function-based views for campaigns, leads, and CSV uploads.
"""

from django.urls import path
from . import views

urlpatterns = [
    path('agent/login/', views.agent_login, name='agent-login'),
    path('agent/logout/', views.logout_agent_api, name='agent-logout'),
    path('dashboard/', views.agent_dashboard, name='agent-dashboard'),
    
    # Call management endpoints
    path('call/', views.initiate_call, name='initiate-call'),
    path('call/<str:uuid>/', views.poll_call_status, name='poll-call-status'),
    path('call-recordings/<str:uuid>/', views.get_call_recording, name='get-call-recording'),
    
    # Campaign endpoints
    
]
