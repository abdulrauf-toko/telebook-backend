import os

from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path
from django.urls import reverse
from django import forms
from django.utils.html import format_html
from django.contrib import messages
from .models import Agent, CallLog, CallLogExports, Campaign, Lead, Team, BulkLeadImport
from .csv_import_utils import process_csv_import

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


# ==================== CSV Import Admin ====================

class BulkLeadImportForm(forms.ModelForm):
    """Custom form for CSV import with file upload and campaign type selection"""
    
    csv_file = forms.FileField(
        label='CSV File',
        help_text='Upload a CSV file with columns: Agent Username, Phone Number, Address, City',
        required=False,
    )
    
    class Meta:
        model = BulkLeadImport
        fields = ['campaign_type']
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Hide auto-generated fields for display purposes
        if self.instance.pk:
            # Show details for existing imports
            self.fields.pop('csv_file', None)


@admin.register(BulkLeadImport)
class BulkLeadImportAdmin(admin.ModelAdmin):
    """Admin interface for bulk lead imports via CSV"""
    
    form = BulkLeadImportForm
    
    list_display = ('id', 'campaign_type', 'status', 'total_records', 'processed_records', 
                    'campaigns_created', 'leads_created', 'leads_updated', 'created_at', 'view_details')
    list_filter = ('status', 'campaign_type', 'created_at')
    readonly_fields = ('status', 'total_records', 'processed_records', 'campaigns_created', 
                      'leads_created', 'leads_updated', 'error_message', 'details_display', 
                      'created_at', 'updated_at')
    
    fieldsets = (
        ('Import Configuration', {
            'fields': ('campaign_type', 'csv_file'),
            'classes': ('wide',),
        }),
        ('Results', {
            'fields': ('status', 'total_records', 'processed_records', 'campaigns_created', 
                      'leads_created', 'leads_updated'),
            'classes': ('collapse',),
        }),
        ('Error Details', {
            'fields': ('error_message', 'details_display'),
            'classes': ('collapse',),
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )
    
    def get_fieldsets(self, request, obj=None):
        """Customize fieldsets based on whether we're adding or editing"""
        if obj is None:
            # Adding new import
            return (
                ('Import Configuration', {
                    'fields': ('campaign_type', 'csv_file'),
                    'description': 'Upload a CSV file with columns: Agent Username, Phone Number, Address, City',
                    'classes': ('wide',),
                }),
            )
        else:
            # Editing existing import - show results
            return self.fieldsets
    
    def get_readonly_fields(self, request, obj=None):
        """Make all fields readonly for existing imports"""
        if obj is not None:
            # For editing existing records, make everything readonly
            return self.readonly_fields + ('campaign_type',)
        return self.readonly_fields
    
    def details_display(self, obj):
        """Display formatted error and import details"""
        if not obj or not obj.details:
            return '-'
        
        details = obj.details
        html = '<div style="font-family: monospace; font-size: 12px;">'
        
        if details.get('agent_errors'):
            html += '<h4>Agent Errors:</h4><ul style="margin: 5px 0;">'
            for error in details['agent_errors']:
                html += f'<li><strong>{error.get("agent", "Unknown")}</strong>: {error.get("error", "Unknown error")}</li>'
            html += '</ul>'
        
        if details.get('lead_errors'):
            html += '<h4>Lead Errors (first 10):</h4><ul style="margin: 5px 0;">'
            for error in details['lead_errors'][:10]:
                html += f'<li><strong>{error.get("agent", "")}</strong> | {error.get("phone_number", "")}: {error.get("error", "Unknown error")}</li>'
            if len(details['lead_errors']) > 10:
                html += f'<li>... and {len(details["lead_errors"]) - 10} more errors</li>'
            html += '</ul>'
        
        html += '</div>'
        return format_html(html)
    details_display.short_description = 'Import Details'
    
    def view_details(self, obj):
        """Link to view full details"""
        if obj.status in ['failed', 'completed'] and (obj.details or obj.error_message):
            return format_html('<span style="color: green;">✓ View</span>')
        elif obj.status == 'pending':
            return 'Pending...'
        elif obj.status == 'processing':
            return 'Processing...'
        return '-'
    view_details.short_description = 'Status'
    
    def response_add(self, request, obj, post_url_unchanged=False):
        """Override response after form submission to process CSV"""
        # Check if CSV file was uploaded
        if 'csv_file' in request.FILES:
            csv_file = request.FILES['csv_file']
            campaign_type = obj.campaign_type
            
            if not campaign_type:
                messages.error(request, 'Campaign type is required')
                return super().response_add(request, obj, post_url_unchanged)
            
            try:
                # Process the CSV, passing the already-created import_record
                success, import_record, results = process_csv_import(csv_file, campaign_type, import_record=obj)
                
                if success:
                    messages.success(
                        request,
                        f'✓ Import completed successfully!\n'
                        f'  • Processed: {results["processed_records"]}/{results["total_records"]} records\n'
                        f'  • Campaigns created: {results["campaigns_created"]}\n'
                        f'  • Leads created: {results["leads_created"]}\n'
                        f'  • Leads updated: {results["leads_updated"]}'
                    )
                    # Redirect to the import record details
                    return super().response_change(request, import_record)
                else:
                    messages.error(request, f'Import failed: {results.get("error", "Unknown error")}')
                    return super().response_change(request, import_record)
                    
            except Exception as e:
                messages.error(request, f'Error processing CSV: {str(e)}')
                import logging
                logger = logging.getLogger(__name__)
                logger.exception('Error in CSV import')
        
        return super().response_add(request, obj, post_url_unchanged)
    
    def has_delete_permission(self, request, obj=None):
        """Allow deletion of import records"""
        return True
