from django.contrib import admin

# Register your models here.
from django.contrib import admin
from django.utils.html import format_html
from .models import Agent, CallLog, Campaign, Lead

# Inline for CallLog in Agent admin
class CallLogInline(admin.TabularInline):
    model = CallLog
    extra = 0
    readonly_fields = ('call_id', 'initiated_at', 'status', 'duration_seconds')
    fields = ('call_id', 'initiated_at', 'status', 'duration_seconds', 'to_number')

# Inline for Lead in Campaign admin
class LeadInline(admin.TabularInline):
    model = Lead
    extra = 0
    readonly_fields = ('customer_name', 'phone_number', 'status', 'attempt_count')
    fields = ('customer_name', 'phone_number', 'status', 'attempt_count', 'created_at')

# Admin for Agent
@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ('id', 'extension', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('extension',)
    inlines = [CallLogInline]

# Admin for CallLog
@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ('call_id', 'agent', 'to_number', 'status', 'initiated_at', 'duration_seconds')
    list_filter = ('status', 'agent', 'campaign', 'initiated_at')
    search_fields = ('call_id', 'to_number', 'freeswitch_uuid')
    readonly_fields = ('call_id', 'initiated_at', 'ended_at', 'duration_seconds')
    date_hierarchy = 'initiated_at'

# Admin for Campaign
@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    # list_display = ('campaign_id', 'campaign_name', 'agent', 'segment', 'status', 'created_at')
    # list_filter = ('segment', 'status', 'agent', 'created_at')
    search_fields = ('campaign_id', 'campaign_name', 'agent__extension')
    # readonly_fields = ('created_at', 'updated_at')
    # inlines = [LeadInline]

# Admin for Lead
@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    # list_display = ('id', 'customer_name', 'phone_number', 'campaign', 'status', 'attempt_count', 'created_at')
    # list_filter = ('status', 'campaign', 'customer_segment', 'created_at')
    search_fields = ('customer_name', 'phone_number', 'udhaar_lead_id')
    # readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('id', 'udhaar_lead_id', 'customer_name', 'phone_number', 'city')
        }),
        ('Campaign & Status', {
            'fields': ('campaign', 'status', 'attempt_count', 'max_attempts', 'call_completed', 'lead_contacted')
        }),
        ('Metadata', {
            'fields': ('customer_segment', 'last_call_date', 'last_order_details', 'month_gmv', 'overall_gmv', 'metadata')
        }),
    )
