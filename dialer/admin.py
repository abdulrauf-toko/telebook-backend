import os

from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path
from django.urls import reverse

from django.utils.html import format_html
from .models import Agent, CallLog, CallLogExports, Campaign, Lead, Team

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

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'created_at')
    search_fields = ('name',)

# Admin for Agent
@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ('id', 'is_active', 'created_at')
    list_filter = ('is_active', 'teams', 'created_at')
    filter_horizontal = ('teams',)
    # inlines = [CallLogInline]

# Admin for CallLog
@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ('call_id', 'agent', 'to_number', 'status', 'initiated_at', 'duration_seconds')
    # list_filter = ('status', 'agent', 'campaign', 'initiated_at')
    search_fields = ('call_id', 'lead__phone_number')
    readonly_fields = ('call_id', 'initiated_at', 'ended_at', 'duration_seconds')
    date_hierarchy = 'initiated_at'


@admin.register(CallLogExports)
class CallLogExportsAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'total_recordings', 'exported_recordings', 'created_at', 'download_zip')
    list_filter = ('status', 'created_at')
    readonly_fields = ('status', 'filepath', 'error', 'filters', 'total_recordings', 'exported_recordings', 'created_at', 'updated_at', 'download_zip')
    fields = ('status', 'filepath', 'download_zip', 'error', 'filters', 'total_recordings', 'exported_recordings', 'created_at', 'updated_at')

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                '<int:export_id>/download/',
                self.admin_site.admin_view(self.download_export),
                name='dialer_calllogexports_download',
            ),
        ]
        return custom_urls + urls

    def download_zip(self, obj):
        if not obj or obj.status != 'completed' or not obj.filepath:
            return '-'
        url = reverse('admin:dialer_calllogexports_download', args=[obj.id])
        return format_html('<a class="button" href="{}">Download ZIP</a>', url)
    download_zip.short_description = 'Download'

    def download_export(self, request, export_id):
        export = self.get_object(request, export_id)
        if not export or not export.filepath or not os.path.exists(export.filepath):
            raise Http404('Export file not found')
        return FileResponse(
            open(export.filepath, 'rb'),
            as_attachment=True,
            filename=os.path.basename(export.filepath),
        )


# Admin for Campaign
@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('campaign_id', 'campaign_name', 'agent', 'segment', 'created_at')
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
            'fields': ('udhaar_lead_id', 'customer_name', 'phone_number', 'city', 'follow_up_date', 'follow_up_time', 'comment')
        }),
        ('Campaign & Status', {
            'fields': ('campaign', 'status', 'attempt_count', 'max_attempts', 'call_completed', 'lead_contacted')
        }),
        ('Metadata', {
            'fields': ('customer_segment', 'last_call_date', 'last_order_details', 'month_gmv', 'overall_gmv', 'metadata')
        }),
    )
