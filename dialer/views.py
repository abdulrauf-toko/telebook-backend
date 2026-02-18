"""
API Views for Dialer endpoints.

Simple JSON-based endpoints for campaign management, lead upload, and call tracking.
Replaces Telecard API functionality.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
import orjson as json
import csv
import io
import logging
import re
from events.utils import mark_agent_idle_in_cache, mark_agent_logged_in_cache

from django.contrib.auth import authenticate
from .models import Agent, Campaign, Lead, CallLog

logger = logging.getLogger(__name__)


def _serialize_campaign(campaign):
    """Convert campaign to JSON dict."""
    return {
        'id': campaign.id,
        'campaign_id': campaign.campaign_id,
        'campaign_name': campaign.campaign_name,
        'segment': campaign.segment,
        'agent_id': campaign.agent_id,
        'status': campaign.status,
        'duration_days': campaign.duration_days,
        'created_at': campaign.created_at.isoformat(),
        'started_at': campaign.started_at.isoformat() if campaign.started_at else None,
        'completed_at': campaign.completed_at.isoformat() if campaign.completed_at else None,
        'updated_at': campaign.updated_at.isoformat(),
        'total_leads': campaign.total_leads,
        'called_leads': campaign.called_leads,
        'completed_calls': campaign.completed_calls,
        'failed_calls': campaign.failed_calls,
        'no_answer_calls': campaign.no_answer_calls,
        'metadata': campaign.metadata,
    }


def _serialize_lead(lead):
    """Convert lead to JSON dict."""
    return {
        'id': lead.id,
        'lead_id': lead.lead_id,
        'phone_number': lead.phone_number,
        'customer_name': lead.customer_name,
        'city': lead.city,
        'segment': lead.segment,
        'campaign_id': lead.campaign_id,
        'customer_segment': lead.customer_segment,
        'last_order_date': lead.last_order_date.isoformat() if lead.last_order_date else None,
        'last_call_date': lead.last_call_date.isoformat() if lead.last_call_date else None,
        'follow_up_date': lead.follow_up_date.isoformat() if lead.follow_up_date else None,
        'status': lead.status,
        'attempt_count': lead.attempt_count,
        'max_attempts': lead.max_attempts,
        'metadata': lead.metadata,
        'created_at': lead.created_at.isoformat(),
        'updated_at': lead.updated_at.isoformat(),
    }


def _serialize_import_log(import_log):
    """Convert import log to JSON dict."""
    return {
        'id': import_log.id,
        'import_id': str(import_log.import_id),
        'filename': import_log.filename,
        'file_size_bytes': import_log.file_size_bytes,
        'campaign_id': import_log.campaign_id,
        'status': import_log.status,
        'started_at': import_log.started_at.isoformat() if import_log.started_at else None,
        'completed_at': import_log.completed_at.isoformat() if import_log.completed_at else None,
        'total_rows': import_log.total_rows,
        'successful_imports': import_log.successful_imports,
        'failed_imports': import_log.failed_imports,
        'skipped_rows': import_log.skipped_rows,
        'error_log': import_log.error_log,
        'summary': import_log.summary,
        'created_at': import_log.created_at.isoformat(),
    }


@csrf_exempt
@require_http_methods(["POST"])
def create_campaign(request):
    """
    Create a new campaign.
    
    POST /api/dialer/campaigns/create/
    {
        "campaign_name": "Ahmed Follow Up",
        "segment": "follow_up",
        "agent_id": 1,
        "duration_days": 1,
        "metadata": {}
    }
    """
    try:
        data = json.loads(request.body)
        
        agent_id = data.get('agent_id')
        segment = data.get('segment')
        campaign_name = data.get('campaign_name')
        duration_days = data.get('duration_days', 1)
        
        # Validate required fields
        if not all([agent_id, segment, campaign_name]):
            return JsonResponse(
                {'error': 'Missing required fields: agent_id, segment, campaign_name'},
                status=400
            )
        
        # Get agent
        agent = get_object_or_404(Agent, id=agent_id)
        
        # Generate campaign_id
        timestamp = timezone.now().strftime('%Y%m%d')
        segment_formatted = segment.replace('_', '-').title()
        campaign_id = f"{agent.user.username if agent.user else agent.extension}-{segment_formatted}-{timestamp}"
        
        # Create campaign
        campaign = Campaign.objects.create(
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            segment=segment,
            agent=agent,
            duration_days=duration_days,
            status='draft',
            metadata=data.get('metadata', {})
        )
        
        return JsonResponse(_serialize_campaign(campaign), status=201)
    
    except Agent.DoesNotExist:
        return JsonResponse({'error': 'Agent not found'}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Error creating campaign: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def upload_csv(request):
    """
    Upload CSV file with leads.
    
    POST /api/dialer/csv-upload/
    Form data:
    - campaign_id: Campaign ID (string)
    - segment: Lead segment (string)
    - csv_file: CSV file
    """
    try:
        campaign_id = request.POST.get('campaign_id')
        segment = request.POST.get('segment')
        csv_file = request.FILES.get('csv_file')
        
        # Validate required fields
        if not all([campaign_id, segment, csv_file]):
            return JsonResponse(
                {'error': 'Missing required fields: campaign_id, segment, csv_file'},
                status=400
            )
        
        # Get campaign
        campaign = get_object_or_404(Campaign, campaign_id=campaign_id)
        
        if campaign.segment != segment:
            return JsonResponse(
                {'error': f'Campaign segment mismatch. Expected {campaign.segment}, got {segment}'},
                status=400
            )
        
        # Create import log
        import_log = LeadImportLog.objects.create(
            filename=csv_file.name,
            file_size_bytes=csv_file.size,
            campaign=campaign,
            status='processing',
            started_at=timezone.now(),
        )
        
        # Process CSV
        result = _process_csv(csv_file, campaign, import_log)
        
        return JsonResponse(result, status=200)
    
    except Campaign.DoesNotExist:
        return JsonResponse({'error': 'Campaign not found'}, status=404)
    except Exception as e:
        logger.error(f"Error uploading CSV: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)


def _process_csv(csv_file, campaign, import_log):
    """Process CSV file and create leads."""
    csv_file.file.seek(0)
    text_file = io.TextIOWrapper(csv_file.file, encoding='utf-8')
    reader = csv.DictReader(text_file)
    
    successful = 0
    failed = 0
    skipped = 0
    errors = []
    
    with transaction.atomic():
        for row_num, row in enumerate(reader, start=2):
            try:
                name = row.get('name', '').strip()
                number = row.get('number', '').strip()
                city = row.get('city', '').strip()
                attempts = row.get('attempts', '1').strip()
                
                if not name or not number:
                    errors.append({
                        'row': row_num,
                        'error': 'Missing required field (name or number)'
                    })
                    failed += 1
                    continue
                
                normalized_number = _normalize_phone(number)
                
                if not normalized_number:
                    errors.append({
                        'row': row_num,
                        'number': number,
                        'error': 'Invalid phone number format'
                    })
                    failed += 1
                    continue
                
                if Lead.objects.filter(campaign=campaign, phone_number=normalized_number).exists():
                    errors.append({
                        'row': row_num,
                        'number': normalized_number,
                        'error': 'Duplicate phone number in campaign'
                    })
                    skipped += 1
                    continue
                
                try:
                    max_attempts = max(1, int(attempts))
                except (ValueError, TypeError):
                    max_attempts = 1
                
                lead_id = f"{campaign.campaign_id}-{normalized_number}"
                
                Lead.objects.create(
                    lead_id=lead_id,
                    phone_number=normalized_number,
                    customer_name=name,
                    city=city,
                    segment=campaign.segment,
                    campaign=campaign,
                    status='pending',
                    max_attempts=max_attempts,
                    metadata={
                        'import_log_id': str(import_log.import_id),
                        'imported_at': timezone.now().isoformat(),
                    }
                )
                
                successful += 1
            
            except Exception as e:
                logger.error(f"Error processing row {row_num}: {str(e)}")
                errors.append({
                    'row': row_num,
                    'error': f'Exception: {str(e)}'
                })
                failed += 1
    
    # Update campaign totals
    campaign.total_leads = campaign.leads.count()
    campaign.save()
    
    # Update import log
    import_log.total_rows = successful + failed + skipped
    import_log.successful_imports = successful
    import_log.failed_imports = failed
    import_log.skipped_rows = skipped
    import_log.error_log = errors
    import_log.completed_at = timezone.now()
    
    if failed == 0 and skipped == 0:
        import_log.status = 'completed'
    elif failed > 0 and successful == 0:
        import_log.status = 'failed'
    else:
        import_log.status = 'partial'
    
    summary = (
        f"Processed {import_log.total_rows} rows. "
        f"Successful: {successful}, Failed: {failed}, Skipped: {skipped}"
    )
    import_log.summary = summary
    import_log.save()
    
    return {
        'import_id': str(import_log.import_id),
        'campaign_id': campaign.campaign_id,
        'filename': import_log.filename,
        'status': import_log.status,
        'total_rows': import_log.total_rows,
        'successful_imports': successful,
        'failed_imports': failed,
        'skipped_rows': skipped,
        'success_rate': round(
            (successful / import_log.total_rows * 100)
            if import_log.total_rows > 0 else 0,
            2
        ),
        'errors': errors[:10],
        'summary': summary,
    }


def _normalize_phone(phone):
    """Normalize phone number."""
    if not phone:
        return None
    
    cleaned = re.sub(r'[\s\-]', '', phone)
    
    if cleaned.startswith('ap'):
        cleaned = cleaned[2:]
    elif cleaned.startswith('b'):
        cleaned = cleaned[1:]
    
    if not cleaned.isdigit() or len(cleaned) != 11:
        return None
    
    if not cleaned.startswith('92'):
        return None
    
    return cleaned


@csrf_exempt
@require_http_methods(["GET"])
def get_campaign_stats(request, campaign_id):
    """Get campaign statistics."""
    try:
        campaign = get_object_or_404(Campaign, id=campaign_id)
        leads = campaign.leads.all()
        calls = campaign.calls.all()
        
        return JsonResponse({
            'campaign_id': campaign.campaign_id,
            'total_leads': campaign.total_leads,
            'pending_leads': leads.filter(status='pending').count(),
            'called_leads': campaign.called_leads,
            'completed_calls': campaign.completed_calls,
            'failed_calls': campaign.failed_calls,
            'no_answer_calls': campaign.no_answer_calls,
            'total_calls': calls.count(),
            'completion_rate': round(
                (campaign.called_leads / campaign.total_leads * 100) 
                if campaign.total_leads > 0 else 0,
                2
            ),
            'success_rate': round(
                (campaign.completed_calls / campaign.called_leads * 100)
                if campaign.called_leads > 0 else 0,
                2
            ),
        })
    except Campaign.DoesNotExist:
        return JsonResponse({'error': 'Campaign not found'}, status=404)


@csrf_exempt
@require_http_methods(["POST"])
def activate_campaign(request, campaign_id):
    """Activate a campaign."""
    try:
        campaign = get_object_or_404(Campaign, id=campaign_id)
        
        if campaign.status == 'active':
            return JsonResponse(
                {'error': 'Campaign is already active'},
                status=400
            )
        
        campaign.status = 'active'
        campaign.started_at = timezone.now()
        campaign.save()
        
        return JsonResponse({
            'message': 'Campaign activated',
            'campaign': _serialize_campaign(campaign)
        })
    except Campaign.DoesNotExist:
        return JsonResponse({'error': 'Campaign not found'}, status=404)


@csrf_exempt
@require_http_methods(["GET"])
def list_leads(request):
    """List leads with filtering."""
    try:
        campaign_id = request.GET.get('campaign_id')
        segment = request.GET.get('segment')
        status_filter = request.GET.get('status')
        
        leads = Lead.objects.all()
        
        if campaign_id:
            leads = leads.filter(campaign_id=campaign_id)
        if segment:
            leads = leads.filter(segment=segment)
        if status_filter:
            leads = leads.filter(status=status_filter)
        
        leads = leads.order_by('segment', '-created_at')[:1000]
        
        return JsonResponse({
            'count': leads.count(),
            'results': [_serialize_lead(lead) for lead in leads]
        })
    except Exception as e:
        logger.error(f"Error listing leads: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def list_campaigns(request):
    """List campaigns with filtering."""
    try:
        agent_id = request.GET.get('agent_id')
        segment = request.GET.get('segment')
        status_filter = request.GET.get('status')
        
        campaigns = Campaign.objects.all()
        
        if agent_id:
            campaigns = campaigns.filter(agent_id=agent_id)
        if segment:
            campaigns = campaigns.filter(segment=segment)
        if status_filter:
            campaigns = campaigns.filter(status=status_filter)
        
        campaigns = campaigns.order_by('-created_at')[:1000]
        
        return JsonResponse({
            'count': campaigns.count(),
            'results': [_serialize_campaign(campaign) for campaign in campaigns]
        })
    except Exception as e:
        logger.error(f"Error listing campaigns: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)



@csrf_exempt  # Disable CSRF for simplicity (use proper auth in production)
def agent_login(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            username = data.get('username')
            password = data.get('password')
        except Exception as e:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        user = authenticate(request, username=username, password=password)
        if user is not None:
            try:
                agent = Agent.objects.get(user=user)
                mark_agent_logged_in_cache(str(agent.id), 'sales')
                
                return JsonResponse({'extension': agent.extension, 'password': "uncharted3", 'id': agent.id})
            except Agent.DoesNotExist:
                return JsonResponse({'error': 'Agent not found'}, status=404)
        else:
            return JsonResponse({'error': 'Invalid credentials'}, status=401)

    return JsonResponse({'error': 'Method not allowed'}, status=405)
