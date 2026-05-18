"""
CSV import utilities for bulk lead and campaign creation.

Handles parsing CSV files and creating campaigns/leads with proper
relationship management and error handling.
"""

import csv
import io
from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Agent, Campaign, Lead, BulkLeadImport
import logging

logger = logging.getLogger(__name__)


class CSVImportError(Exception):
    """Base exception for CSV import errors"""
    pass


class AgentNotFoundError(CSVImportError):
    """Raised when agent username is not found"""
    pass


class PhoneNumberValidationError(CSVImportError):
    """Raised when phone number validation fails"""
    pass


def normalize_phone_number(phone_number):
    """
    Normalize phone number to standard format.
    
    Handles:
    - +92 prefix → converts to 0
    - 92 prefix → converts to 0
    - Removes spaces, brackets, and dashes
    - Works with both mobile (03xx) and landline numbers
    
    Examples:
    - 03001234567 -> 03001234567
    - +923001234567 -> 03001234567
    - 923001234567 -> 03001234567
    - 03-001-234567 -> 03001234567
    - (300) 123-4567 -> 03001234567
    
    Args:
        phone_number: Phone number string in any format
        
    Returns:
        str: Normalized phone number
        
    Raises:
        PhoneNumberValidationError: If phone number is empty or invalid
    """
    if not phone_number or not isinstance(phone_number, str):
        raise PhoneNumberValidationError("Phone number must be a non-empty string")
    
    # Remove spaces, brackets, and dashes
    normalized = phone_number.strip().replace(' ', '').replace('(', '').replace(')', '').replace('-', '')
    
    # Handle +92 prefix
    if normalized.startswith('+92'):
        normalized = '0' + normalized[3:]
    # Handle 92 prefix (but not 920+, which is already a valid format)
    elif normalized.startswith('92') and not normalized.startswith('920'):
        normalized = '0' + normalized[2:]
    # Prepend 0 for numbers that don't start with 0
    elif not normalized.startswith('0'):
        normalized = '0' + normalized
    
    if not normalized:
        raise PhoneNumberValidationError(f"Phone number '{phone_number}' is empty after normalization")
    
    return normalized


def process_csv_import(csv_file, campaign_type, segment='other', import_record=None):
    """
    Process CSV file and create campaigns and leads.
    
    Args:
        csv_file: File object or bytes containing CSV data
        campaign_type: Campaign type selected (e.g., 'saddar_growth', 'rupin_emi', 'oscar')
        segment: Lead segment classification (default: 'follow_up')
        import_record: Optional existing BulkLeadImport record to update (if None, creates new one)
        
    Returns:
        tuple: (success, import_record, results_dict)
        - success: Boolean indicating if import succeeded
        - import_record: BulkLeadImport model instance
        - results_dict: Dictionary with detailed results including errors
    """
    
    # Use existing record or create new one
    if import_record is None:
        import_record = BulkLeadImport.objects.create(
            campaign_type=campaign_type,
            status='processing'
        )
    else:
        import_record.status = 'processing'
        import_record.save()
    
    results = {
        'total_records': 0,
        'processed_records': 0,
        'campaigns_created': 0,
        'leads_created': 0,
        'leads_updated': 0,
        'agent_errors': [],
        'lead_errors': [],
    }
    
    try:
        # Parse CSV
        if isinstance(csv_file, bytes):
            csv_content = csv_file.decode('utf-8')
        else:
            csv_content = csv_file.read().decode('utf-8')
        
        csv_reader = csv.DictReader(io.StringIO(csv_content))
        
        # Validate headers
        required_headers = {'Agent Username', 'Phone Number', 'Address', 'City', 'Name'}
        if not csv_reader.fieldnames:
            raise CSVImportError("CSV file is empty")
        
        csv_headers = set(csv_reader.fieldnames)
        missing_headers = required_headers - csv_headers
        if missing_headers:
            raise CSVImportError(f"Missing required columns: {', '.join(missing_headers)}")
        
        rows_list = list(csv_reader)
        results['total_records'] = len(rows_list)
        import_record.total_records = len(rows_list)
        import_record.save()
        
        # Group by agent to batch campaign creation
        agent_groups = {}
        for idx, row in enumerate(rows_list, 1):
            agent_username = row.get('Agent Username', '').strip()
            phone_number = row.get('Phone Number', '').strip()
            
            # Validate required fields
            if not agent_username:
                results['agent_errors'].append({
                    'row': idx,
                    'error': 'Missing Agent Username'
                })
                continue
            
            if not phone_number:
                results['lead_errors'].append({
                    'row': idx,
                    'agent': agent_username,
                    'error': 'Missing Phone Number'
                })
                continue
            
            if agent_username not in agent_groups:
                agent_groups[agent_username] = []
            agent_groups[agent_username].append((idx, row))
        
        # Process each agent
        today = timezone.now().date()
        campaigns_by_agent = {}  # Cache for campaign creation
        
        with transaction.atomic():
            for agent_username, rows in agent_groups.items():
                try:
                    # Get agent by username through User model
                    try:
                        user = User.objects.get(username=agent_username)
                        agent = Agent.objects.get(user=user)
                    except User.DoesNotExist:
                        raise AgentNotFoundError(
                            f"User with username '{agent_username}' not found in system"
                        )
                    except Agent.DoesNotExist:
                        raise AgentNotFoundError(
                            f"Agent profile not found for user '{agent_username}'"
                        )
                    
                    # Get or create campaign for this agent on today's date
                    campaign_key = f"{agent.id}_{segment}_{today}"
                    
                    if campaign_key not in campaigns_by_agent:
                        campaign, created = Campaign.objects.get_or_create(
                            agent=agent,
                            segment=segment,
                            created_at__date=today,
                            defaults={
                                'campaign_id': f"{agent.extension}-{segment}-{today.strftime('%Y%m%d')}",
                                'campaign_name': f"{agent.user.username} - {segment} - {today}",
                                'campaign_type': campaign_type,
                                'active': True,
                            }
                        )
                        campaigns_by_agent[campaign_key] = campaign
                        if created:
                            results['campaigns_created'] += 1
                    else:
                        campaign = campaigns_by_agent[campaign_key]
                    
                    # Process leads for this agent
                    for row_idx, row in rows:
                        try:
                            phone_number = row.get('Phone Number', '').strip()
                            name = row.get('Name', '').strip()
                            address = row.get('Address', '').strip()
                            city = row.get('City', '').strip()
                            
                            # Validate phone number is not empty (should be caught earlier, but double-check)
                            if not phone_number:
                                results['lead_errors'].append({
                                    'row': row_idx,
                                    'agent': agent_username,
                                    'error': 'Phone Number is empty'
                                })
                                continue
                            
                            # Skip if phone contains alphabets
                            if any(c.isalpha() for c in phone_number):
                                results['lead_errors'].append({
                                    'row': row_idx,
                                    'agent': agent_username,
                                    'phone_number': phone_number,
                                    'error': f"Invalid phone number '{phone_number}': contains alphabetic characters"
                                })
                                continue

                            # Normalize phone number
                            try:
                                normalized_phone = normalize_phone_number(phone_number)
                            except PhoneNumberValidationError as e:
                                results['lead_errors'].append({
                                    'row': row_idx,
                                    'agent': agent_username,
                                    'phone_number': phone_number,
                                    'error': str(e)
                                })
                                continue

                            # Use latest existing lead for this phone number, if duplicates exist.
                            lead = Lead.objects.filter(phone_number=normalized_phone).order_by('-created_at', '-id').first()
                            if lead is None:
                                lead = Lead.objects.create(
                                    phone_number=normalized_phone,
                                    customer_name=name or normalized_phone,
                                    address=address,
                                    city=city,
                                    status='pending',
                                    attempt_count=0,
                                    max_attempts=1,
                                    campaign=campaign,
                                )
                                created = True
                            else:
                                created = False
                                if name and lead.customer_name != name:
                                    lead.customer_name = name
                                if address and lead.address != address:
                                    lead.address = address
                                if city and lead.city != city:
                                    lead.city = city
                                if lead.campaign != campaign:
                                    lead.campaign = campaign
                                
                                lead.status = 'pending'
                                lead.save()

                            if created:
                                results['leads_created'] += 1
                            else:
                                results['leads_updated'] += 1
                            
                            results['processed_records'] += 1
                            
                        except Exception as e:
                            results['lead_errors'].append({
                                'row': row_idx,
                                'agent': agent_username,
                                'phone_number': row.get('Phone Number', ''),
                                'error': str(e)
                            })
                            logger.error(f"Error processing lead for agent {agent_username}: {e}")
                
                except AgentNotFoundError as e:
                    results['agent_errors'].append({
                        'agent': agent_username,
                        'error': str(e)
                    })
                    logger.warning(f"Agent not found: {agent_username}")
                except Exception as e:
                    results['agent_errors'].append({
                        'agent': agent_username,
                        'error': str(e)
                    })
                    logger.error(f"Error processing agent {agent_username}: {e}")
        
        # Update import record
        import_record.status = 'completed'
        import_record.processed_records = results['processed_records']
        import_record.campaigns_created = results['campaigns_created']
        import_record.leads_created = results['leads_created']
        import_record.leads_updated = results['leads_updated']
        import_record.details = {
            'agent_errors': results['agent_errors'],
            'lead_errors': results['lead_errors'],
        }
        import_record.save()
        
        return True, import_record, results
        
    except CSVImportError as e:
        import_record.status = 'failed'
        import_record.error_message = str(e)
        import_record.save()
        logger.error(f"CSV Import Error: {e}")
        return False, import_record, {'error': str(e)}
    except Exception as e:
        import_record.status = 'failed'
        import_record.error_message = f"Unexpected error: {str(e)}"
        import_record.save()
        logger.error(f"Unexpected error in CSV import: {e}")
        return False, import_record, {'error': str(e)}
