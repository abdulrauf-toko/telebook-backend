"""
URL configuration for Dialer API endpoints.

Simple function-based views for campaigns, leads, and CSV uploads.
"""

from django.urls import path
from . import views

urlpatterns = [
    path('agent/login/', views.agent_login, name='agent-login'),
    # Campaign endpoints
    path('campaigns/create/', views.create_campaign, name='create_campaign'),
    path('campaigns/', views.list_campaigns, name='list_campaigns'),
    path('campaigns/<int:campaign_id>/stats/', views.get_campaign_stats, name='campaign_stats'),
    path('campaigns/<int:campaign_id>/activate/', views.activate_campaign, name='activate_campaign'),
    
    # Lead endpoints
    path('leads/', views.list_leads, name='list_leads'),
    
    # CSV upload endpoint
    path('csv-upload/', views.upload_csv, name='upload_csv'),
]
