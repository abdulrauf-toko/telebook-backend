import uuid

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.shortcuts import render
import orjson as json
import logging
from datetime import date
import pytz
from events.utils import mark_agent_logged_in_cache, logout_agent, add_active_call_in_cache

from django.contrib.auth import authenticate, login, logout
from voice_orchestrator.freeswitch import fs_manager
from voice_orchestrator.redis import ACTIVE_CALL_LOCK_REDIS_KEY, ACTIVE_CALLS_REDIS_KEY, COMPLETED_CALLS_REDIS_KEY, LOCK_TIMEOUTS, SLEEP, conn
from .models import Agent, Lead, CallLog
from dialer.utils import build_originate_command, originate_call, get_disposition_mapping

logger = logging.getLogger(__name__)


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
                login(request, user)
                agent = Agent.objects.get(user=user)
                
                group = user.groups.first()
                team = group.name
                mark_agent_logged_in_cache(str(agent.id), team)
                all_agents = Agent.objects.values('user__username', 'extension').exclude(id=agent.id)
                all_agents = list(all_agents)
                
                return JsonResponse({'extension': agent.extension, 'password': agent.freeswitch_password, 'id': agent.id, "agents_info": list(all_agents)})
            except Agent.DoesNotExist:
                return JsonResponse({'error': 'Agent not found'}, status=404)
        else:
            return JsonResponse({'error': 'Invalid credentials'}, status=401)

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@require_http_methods(["POST"])
def logout_agent_api(request):
    try:
        user = request.user
        try:
            agent = user.agent
        except Agent.DoesNotExist:
            return JsonResponse({'error': 'User is not an agent'}, status=400)

        agent_id = str(agent.id)

        result = logout_agent(agent_id)

        logout(request)

        if result:
            return JsonResponse({'status': 'success', 'agent_id': agent_id})
        else:
            return JsonResponse({'status': 'partial_success', 'note': 'Session cleared but cache cleanup failed'}, status=500)

    except Exception as e:
        logger.exception(f"Logout error: {e}")
        return JsonResponse({'error': 'Internal server error'}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def do_not_disturb(request):
    try:
        user = request.user
        try:
            agent = user.agent
        except Agent.DoesNotExist:
            return JsonResponse({'error': 'User is not an agent'}, status=400)

        agent_id = str(agent.id)

        result = logout_agent(agent_id) #handle like log out in cache 

        # logout(request)

        if result:
            return JsonResponse({'status': 'success', 'agent_id': agent_id})
        else:
            return JsonResponse({'status': 'partial_success', 'note': 'Session cleared but cache cleanup failed'}, status=500)

    except Exception as e:
        logger.exception(f"Logout error: {e}")
        return JsonResponse({'error': 'Internal server error'}, status=500)
    


@csrf_exempt
@require_http_methods(["POST"])
def mark_available(request):
    try:
        user = request.user
        try:
            agent = user.agent
        except Agent.DoesNotExist:
            return JsonResponse({'error': 'User is not an agent'}, status=400)

        agent_id = str(agent.id)
        group = user.groups.first()
        team = group.name

        mark_agent_logged_in_cache(str(agent.id), team)
        return JsonResponse({'status': 'success', 'agent_id': agent_id})

    except Exception as e:
        logger.exception(f"Logout error: {e}")
        return JsonResponse({'error': 'Internal server error'}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def initiate_call(request):
    try:
        data = json.loads(request.body)
        phone_number = data.get('phone_number')
        username = data.get('username')
        
        if not phone_number or not username:
            return JsonResponse({
                'success': False,
                'message': 'phone_number and username are required'
            }, status=400)
        
        # Validate phone number format (should start with 03xx)
        if not phone_number.startswith('03') or len(phone_number) != 11:
            return JsonResponse({
                'success': False,
                'message': 'Invalid phone number format. Should start with 03xx and be 11 digits'
            }, status=400)
        
        try:
            agent = Agent.objects.get(udhaar_username=username)
        except (Agent.DoesNotExist):
            return JsonResponse({
                'success': False,
                'message': 'user not registered on softphone'
            }, status=404)
        
        # Check if agent is active
        if not agent.is_active:
            return JsonResponse({
                'success': False,
                'message': 'Agent is not active'
            }, status=400)
        
        payload = {
            'manual_trigger': 'true'
        }

        call_uuid = str(uuid.uuid4())      
        
        originate_command = build_originate_command(
            call_id=call_uuid,
            phone_number=phone_number,
            agent_id=agent.id,
            payload=payload,
            auto_bridge=True,
            park=False
        )
        fs_manager.bgapi(originate_command)

        # if not success:
        #     return JsonResponse({
        #         'success': False,
        #         'message': 'Failed to originate call'
        #     }, status=400)
        
        add_active_call_in_cache(call_uuid, {
                "agent_id": agent.id,
                "phone_number": phone_number,
                "payload": payload,
                "call_uuid": call_uuid,
                "initiated_at": timezone.now().isoformat()
            })
        
        logger.info(f"Call initiated: {call_uuid} from agent {username} to {phone_number}")
        
        return JsonResponse({
            'success': True,
            'call_uuid': call_uuid
        })
        
    except json.JSONDecodeError:
        return JsonResponse({
            'success': False,
            'message': 'Invalid JSON in request body'
        }, status=400)
    except Exception as e:
        logger.exception(f"Error initiating call: {e}")
        return JsonResponse({
            'success': False,
            'message': 'Internal server error'
        }, status=500)


@require_http_methods(["GET"])
def poll_call_status(request, uuid):
    lock_key = f"{ACTIVE_CALL_LOCK_REDIS_KEY}{uuid}"
    call_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    try:
        if call_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            # Get the call log
            raw_data = conn.hget(ACTIVE_CALLS_REDIS_KEY, uuid)
            if raw_data:
                return JsonResponse({
                    "success": True,
                    "status": "pending"
                })
        else:
            logger.error(f"Could not acquire lock for call {uuid} while polling - System Busy")
            return JsonResponse({
                    "success": False,
                    "message": "Error acquiring lock"
                }, status=500)
    except Exception as e:
        logger.exception(f"Error Getting data from redis for call {uuid} while polling: {e}")
    finally:
        if call_lock.owned():
            call_lock.release()

    lock_key = f"{COMPLETED_CALLS_REDIS_KEY}:lock"
    call_lock = conn.lock(lock_key, timeout=LOCK_TIMEOUTS, sleep=SLEEP)
    try:
        if call_lock.acquire(blocking_timeout=LOCK_TIMEOUTS):
            # Get the call log
            raw_calls = conn.lrange(COMPLETED_CALLS_REDIS_KEY, 0, -1)
                
        else:
            logger.error(f"Could not acquire lock for call {uuid} while polling - System Busy")
            return JsonResponse({
                    "success": False,
                    "message": "Error acquiring lock"
                }, status=500)
    except Exception as e:
        logger.exception(f"Error Getting data from redis for call {uuid} while polling: {e}")
        return JsonResponse({
                    "success": False,
                    "message": "Error getting data from redis"
                }, status=500)
    finally:
        if call_lock.owned():
            call_lock.release()

    completed_calls = [json.loads(call) for call in raw_calls]
    for call in completed_calls:
        if call.get('call_uuid') == uuid:
            call_disposition = get_disposition_mapping(call.get('disconnect_reason'))
            if call_disposition == 'failed':
                return JsonResponse({
                    "success": False,
                    "status": 'failed',
                    "message": call.get('disconnect_reason')
                })
            return JsonResponse({
                    "success": True,
                    "status": 'completed',
                    "call_disposition": call_disposition
                })
    
    try:
        data = CallLog.objects.get(call_id=uuid)
        if data.status == 'Answered':
            call_disposition = 'answered'
        elif data.status == 'No Answer':
            call_disposition = 'not_answered'
        elif data.status == 'busy':
            call_disposition = 'busy'
        elif data.status == 'failed':
            call_disposition = 'failed'
            return JsonResponse({
                "success": False,
                "status": 'failed',
                "message": data.disconnect_reason
            }, status=404)    
        else:
            call_disposition = 'unknown'
        return JsonResponse({
                "success": True,
                "status": 'completed',
                "call_disposition": call_disposition
            })        
    except CallLog.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Call not found'
        }, status=404)
        


@require_http_methods(["GET"])
def get_call_recording(request, uuid):
    """
    Fetches the recording URL for a completed call.
    
    GET /call-recordings/{uuid}
    Response: {"success": true, "recording_url": "https://..."} or {"success": false, "message": "error"}
    """
    try:
        # Get the call log
        try:
            call_log = CallLog.objects.get(call_id=uuid)
        except CallLog.DoesNotExist:
            return JsonResponse({
                'success': False,
                'message': 'Call not found'
            }, status=404)
        
        # Check if call has a recording
        if not call_log.recording_url:
            return JsonResponse({
                'success': False,
                'message': 'Recording not available for this call'
            }, status=404)
        
        return JsonResponse({
            'success': True,
            'recording_url': call_log.recording_url
        })
        
    except Exception as e:
        logger.exception(f"Error getting call recording for {uuid}: {e}")
        return JsonResponse({
            'success': False,
            'message': 'Internal server error'
        }, status=500)


def agent_dashboard(request):
    """
    Dashboard view for agents to see their call logs and leads.
    Shows call logs for a selected date (defaults to today), total call logs, and total leads assigned.
    """
    username = request.GET.get('username') or request.POST.get('username')
    date_filter = request.GET.get('date_filter') or request.POST.get('date_filter')
    
    agent = None
    call_logs = []
    unique_phone_count = 0
    today_leads_count = 0
    error_message = None
    stats = {}
    selected_date = date.today()
    
    # Parse date_filter if provided
    if date_filter:
        try:
            selected_date = date.fromisoformat(date_filter)
        except (ValueError, TypeError):
            selected_date = date.today()
    
    if username:
        try:
            from django.contrib.auth.models import User
            user = User.objects.get(username=username)
            agent = Agent.objects.get(user=user)
            
            # Get all call logs for selected date for this agent
            call_logs = CallLog.objects.filter(
                agent=agent,
                initiated_at__date=selected_date
            ).select_related('lead', 'campaign').order_by('-initiated_at')
            
            # Convert call logs to Karachi timezone
            karachi_tz = pytz.timezone('Asia/Karachi')
            for log in call_logs:
                if log.initiated_at:
                    log.initiated_at_karachi = log.initiated_at.astimezone(karachi_tz)
                else:
                    log.initiated_at_karachi = None
            
            # Count unique phone numbers called
            unique_phone_count = call_logs.values('to_number').distinct().count()
            
            # Get leads assigned to this agent on selected date
            leads_on_date = Lead.objects.filter(
                campaign__active=True,
                campaign__created_at__date=selected_date,
                campaign__agent=agent
            )
            today_leads_count = leads_on_date.count()
            
            
            # Calculate statistics
            stats = {
                'total_calls': CallLog.objects.filter(agent=agent).count(),
                'answered': call_logs.filter(status='answered').count(),
                'failed': call_logs.filter(status='failed').count(),
                'no_answer': call_logs.filter(status='no_answer').count(),
                'busy': call_logs.filter(status='busy').count(),
                'total_talk_time': sum(log.talk_time_seconds for log in call_logs),
            }
            
        except User.DoesNotExist:
            error_message = f"User '{username}' not found."
        except Agent.DoesNotExist:
            error_message = f"Agent not found for user '{username}'."
        except Exception as e:
            error_message = f"An error occurred: {str(e)}"
    
    context = {
        'username': username,
        'agent': agent,
        'call_logs': call_logs,
        'unique_phone_count': unique_phone_count,
        'today_leads_count': today_leads_count,
        'error_message': error_message,
        'stats': stats,
        'selected_date': selected_date.isoformat(),
    }
    
    return render(request, 'dialer/dashboard.html', context)
    

