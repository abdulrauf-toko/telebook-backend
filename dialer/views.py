import uuid

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.shortcuts import render
import orjson as json
import logging
from datetime import date, datetime, timedelta
import pytz
from events.utils import mark_agent_logged_in_cache, logout_agent, add_active_call_in_cache, log_agent_authentication_action
from events.models import AgentLogs
from django.contrib.auth.decorators import login_required

from django.contrib.auth import authenticate, login, logout
from voice_orchestrator.freeswitch import fs_manager
from voice_orchestrator.redis import ACTIVE_CALL_LOCK_REDIS_KEY, ACTIVE_CALLS_REDIS_KEY, AGENT_STATE_REDIS_KEY, COMPLETED_CALLS_REDIS_KEY, LOCK_TIMEOUTS, SLEEP, conn
from voice_orchestrator.utils import generate_presigned_s3_url
from .models import Agent, Campaign, Lead, CallLog
from dialer.utils import build_originate_command, get_disposition_mapping, active_campaigns
from dialer.tasks import formdata_scheduled_task

logger = logging.getLogger(__name__)
PKT = pytz.timezone("Asia/Karachi")


def _format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _safe_load_json(raw_data):
    if not raw_data:
        return {}

    try:
        return json.loads(raw_data)
    except Exception:
        return {}


def _get_agent_summary_data(agent_id, target_date):
    logs = (
        AgentLogs.objects.filter(
            agent_id=agent_id,
            created_at__date=target_date,
            action__in=['login', 'logout']
        )
        .order_by('created_at')
    )

    if not logs.exists():
        return None

    login_times = []
    logout_times = []

    total_logged_out = timedelta()
    current_logout_time = None

    for log in logs:
        pkt_time = log.created_at.astimezone(PKT)

        if log.action == "login":
            login_times.append(pkt_time)

            if current_logout_time:
                total_logged_out += pkt_time - current_logout_time
                current_logout_time = None

        elif log.action == "logout":
            logout_times.append(pkt_time)
            current_logout_time = pkt_time

    if not login_times:
        return None

    first_login = login_times[0]
    last_logout = logout_times[-1] if logout_times else login_times[-1]

    return {
        "agent_id": agent_id,
        "first_login": first_login,
        "last_logout": last_logout,
        "total_logged_out": total_logged_out
    }


@csrf_exempt  # Disable CSRF for simplicity (use proper auth in production)
def agent_login(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            username = data.get('username')
            password = data.get('password')
        except Exception as e:
            return JsonResponse({'success': False, 'message': 'Invalid JSON'}, status=400)
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            try:
                login(request, user)
                agent = Agent.objects.get(user=user)
                
                group = user.groups.first()
                team = group.name
                mark_agent_logged_in_cache(str(agent.id), team)
                log_agent_authentication_action(agent.id, 'login')
                all_agents = Agent.objects.values('user__username', 'extension').exclude(id=agent.id)
                all_agents = list(all_agents)
                campaigns = active_campaigns(agent)
                
                return JsonResponse({'success': True, 'extension': agent.extension, 'password': agent.freeswitch_password, 'id': agent.id, "agents_info": list(all_agents), "campaigns": campaigns})
            except Agent.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Invalid credentials'}, status=404)
        else:
            return JsonResponse({'success': False, 'message': 'Invalid credentials'}, status=401)

    return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

@csrf_exempt
@require_http_methods(["POST"])
def logout_agent_api(request):
    try:
        data = json.loads(request.body)
        agent_id = data.get('agent_id')

        agent_id = str(agent_id)

        result = logout_agent(agent_id)

        logout(request)
        log_agent_authentication_action(int(agent_id), 'logout')

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
@require_http_methods(["POST", "OPTIONS"])
def initiate_call(request):
    try:
        data = json.loads(request.body)
        phone_number = data.get('phone_number')
        username = data.get('username')

        logger.info(f"Initiating call to {phone_number} for agent {username}")
        
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
            auto_bridge=True
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


@require_http_methods(["GET", "OPTIONS"])
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
            if call_disposition == 'user_not_registered' or call_disposition == 'failed' or call_disposition == 'lose_race' or call_disposition == 'invalid':
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
    Generates a short-lived recording URL for a completed call.
    
    GET /call-recordings/{uuid}
    Response: {"success": true, "recording_url": "https://..."} or {"success": false, "message": "error"}
    """
    try:
        # Get the call log
        try:
            logger.info(uuid)
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
        
        signed_url = generate_presigned_s3_url(call_log.recording_url)

        return JsonResponse({
            'success': True,
            'recording_url': signed_url
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
    active_campaigns = []
    error_message = None
    stats = {}
    karachi_tz = pytz.timezone('Asia/Karachi')
    now_pk = timezone.now().astimezone(karachi_tz)
    selected_date = now_pk.date()
    logged_out_minutes = 0
    
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

            day_start_pk = karachi_tz.localize(datetime.combine(selected_date, datetime.min.time()))
            if selected_date == now_pk.date():
                day_end_pk = now_pk
            else:
                day_end_pk = karachi_tz.localize(datetime.combine(selected_date, datetime.max.time().replace(microsecond=0)))

            day_start_utc = day_start_pk.astimezone(pytz.utc)
            day_end_utc = day_end_pk.astimezone(pytz.utc)
            
            # Get all call logs for the selected Pakistan-time day for this agent.
            call_logs = CallLog.objects.filter(
                agent=agent,
                initiated_at__gte=day_start_utc,
                initiated_at__lte=day_end_utc,
            ).select_related('lead', 'campaign').order_by('-initiated_at')
            
            # Convert call logs to Karachi timezone
            for log in call_logs:
                if log.initiated_at:
                    log.initiated_at_karachi = log.initiated_at.astimezone(karachi_tz)
                else:
                    log.initiated_at_karachi = None
                if log.answered_at:
                    log.answered_at_karachi = log.answered_at.astimezone(karachi_tz)
                else:
                    log.answered_at_karachi = None
                if log.ended_at:
                    log.ended_at_karachi = log.ended_at.astimezone(karachi_tz)
                else:
                    log.ended_at_karachi = None
                log.call_uuid = log.call_id
                log.has_call_recording = bool(
                    log.status == 'answered'
                    and log.recording_url
                    and log.recording_url.startswith('https://')
                )
            
            # Count unique phone numbers called
            unique_phone_count = 0
            unique_phone_set = set()
            for call_log in call_logs:
                if call_log.lead and call_log.lead.phone_number:
                    unique_phone_set.add(call_log.lead.phone_number)
                elif call_log.to_number:
                    unique_phone_set.add(call_log.to_number)
            unique_phone_count = len(unique_phone_set)

            
            # Get leads assigned to this agent on selected date
            leads_on_date = Lead.objects.filter(
                campaign__active=True,
                campaign__created_at__date=selected_date,
                campaign__agent=agent
            )
            today_leads_count = leads_on_date.count()

            # Active campaigns for this agent with lead counts
            from django.db.models import Count, Q
            active_campaigns = (
                Campaign.objects.filter(agent=agent, active=True)
                .annotate(
                    total_leads=Count('leads'),
                    pending_leads=Count('leads', filter=Q(leads__status='pending')),
                )
                .values('campaign_id', 'segment', 'total_leads', 'pending_leads')
                .order_by('segment')
            )
            
            
            # Calculate statistics
            stats = {
                'total_calls': CallLog.objects.filter(agent=agent).count(),
                'answered': call_logs.filter(status='answered').count(),
                'failed': call_logs.filter(status='failed').count(),
                'no_answer': call_logs.filter(status='no_answer').count(),
                'busy': call_logs.filter(status='busy').count(),
                'total_talk_time': sum(log.talk_time_seconds for log in call_logs),
            }

            agent_summary = _get_agent_summary_data(agent.id, selected_date)
            if agent_summary:
                logged_out_minutes = round(agent_summary["total_logged_out"].total_seconds() / 60, 1)
            
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
        'active_campaigns': active_campaigns,
        'error_message': error_message,
        'stats': stats,
        'selected_date': selected_date.isoformat(),
        'logged_out_minutes': logged_out_minutes,
    }
    
    return render(request, 'dialer/dashboard.html', context)


def all_call_logs_dashboard(request):
    """
    Dashboard view for all call logs in a Pakistan-time date/time range.
    """
    karachi_tz = pytz.timezone('Asia/Karachi')
    now_pk = timezone.now().astimezone(karachi_tz)

    start_date_value = request.GET.get('start_date') or now_pk.date().isoformat()
    start_time_value = request.GET.get('start_time') or '00:00'
    end_date_value = request.GET.get('end_date') or now_pk.date().isoformat()
    end_time_value = request.GET.get('end_time') or now_pk.strftime('%H:%M')

    call_logs = CallLog.objects.none()
    error_message = None
    unique_phone_count = 0
    stats = {
        'total_calls': 0,
        'answered': 0,
        'failed': 0,
        'no_answer': 0,
        'busy': 0,
        'total_talk_time': 0,
    }

    try:
        start_date = datetime.strptime(start_date_value, '%Y-%m-%d').date()
        start_time = datetime.strptime(start_time_value, '%H:%M').time()
        end_date = datetime.strptime(end_date_value, '%Y-%m-%d').date()
        end_time = datetime.strptime(end_time_value, '%H:%M').time()

        start_pk = karachi_tz.localize(datetime.combine(start_date, start_time))
        end_pk = karachi_tz.localize(datetime.combine(end_date, end_time))

        if start_pk > end_pk:
            error_message = 'Start date/time cannot be after end date/time.'
        else:
            start_utc = start_pk.astimezone(pytz.utc)
            end_utc = end_pk.astimezone(pytz.utc)

            call_logs = CallLog.objects.filter(
                initiated_at__gte=start_utc,
                initiated_at__lte=end_utc,
            ).select_related('agent__user', 'lead', 'campaign').order_by('-initiated_at')

            unique_phone_set = set()
            for log in call_logs:
                if log.initiated_at:
                    log.initiated_at_karachi = log.initiated_at.astimezone(karachi_tz)
                else:
                    log.initiated_at_karachi = None
                log.call_uuid = log.call_id
                log.has_call_recording = bool(
                    log.status == 'answered'
                    and log.recording_url
                    and log.recording_url.startswith('https://')
                )

                if log.lead and log.lead.phone_number:
                    unique_phone_set.add(log.lead.phone_number)
                elif log.to_number:
                    unique_phone_set.add(log.to_number)

            unique_phone_count = len(unique_phone_set)
            stats = {
                'total_calls': call_logs.count(),
                'answered': call_logs.filter(status='answered').count(),
                'failed': call_logs.filter(status='failed').count(),
                'no_answer': call_logs.filter(status='no_answer').count(),
                'busy': call_logs.filter(status='busy').count(),
                'total_talk_time': sum(log.talk_time_seconds for log in call_logs),
            }
    except (TypeError, ValueError):
        error_message = 'Start and end date/time must be valid.'

    context = {
        'call_logs': call_logs,
        'unique_phone_count': unique_phone_count,
        'stats': stats,
        'error_message': error_message,
        'start_date': start_date_value,
        'start_time': start_time_value,
        'end_date': end_date_value,
        'end_time': end_time_value,
    }

    return render(request, 'dialer/all_call_logs_dashboard.html', context)


def manager_dashboard(request):
    """
    Manager dashboard for monitoring live agent availability and daily productivity.
    """
    karachi_tz = pytz.timezone('Asia/Karachi')
    now_pk = timezone.now().astimezone(karachi_tz)

    selected_date_value = request.GET.get('date') or now_pk.date().isoformat()
    error_message = None

    try:
        selected_date = datetime.strptime(selected_date_value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        selected_date = now_pk.date()
        selected_date_value = selected_date.isoformat()
        error_message = 'Date must be valid. Showing today instead.'

    day_start_pk = karachi_tz.localize(datetime.combine(selected_date, datetime.min.time()))
    if selected_date == now_pk.date():
        day_end_pk = now_pk
    else:
        day_end_pk = karachi_tz.localize(datetime.combine(selected_date, datetime.max.time().replace(microsecond=0)))

    day_start_utc = day_start_pk.astimezone(pytz.utc)
    day_end_utc = day_end_pk.astimezone(pytz.utc)

    try:
        raw_agent_states = conn.hgetall(AGENT_STATE_REDIS_KEY)
    except Exception as exc:
        logger.exception(f"Error reading live agent states: {exc}")
        raw_agent_states = {}
        error_message = 'Live Redis status is unavailable. Showing database stats only.'
    live_agent_states = {
        int(agent_id): _safe_load_json(raw_data)
        for agent_id, raw_data in raw_agent_states.items()
        if str(agent_id).isdigit()
    }

    from django.db.models import Count, Q
    call_stats_by_agent = {
        row['agent_id']: row
        for row in (
            CallLog.objects
            .filter(initiated_at__gte=day_start_utc, initiated_at__lte=day_end_utc)
            .values('agent_id')
            .annotate(
                total_calls=Count('id'),
                answered_calls=Count('id', filter=Q(status='answered')),
            )
        )
    }

    logs_by_agent = {}
    logs = (
        AgentLogs.objects
        .filter(created_at__gte=day_start_utc, created_at__lte=day_end_utc)
        .select_related('agent')
        .order_by('agent_id', 'created_at')
    )
    for log in logs:
        logs_by_agent.setdefault(log.agent_id, []).append(log)

    latest_login_logs = {
        log.agent_id: log
        for log in (
            AgentLogs.objects
            .filter(created_at__lte=day_end_utc, action='login')
            .order_by('agent_id', '-created_at')
            .distinct('agent_id')
        )
    }

    latest_logout_logs = {
        log.agent_id: log
        for log in (
            AgentLogs.objects
            .filter(created_at__lte=day_end_utc, action='logout')
            .order_by('agent_id', '-created_at')
            .distinct('agent_id')
        )
    }

    agents = (
        Agent.objects
        .filter(is_active=True)
        .select_related('user', 'selected_campaign')
        .prefetch_related('teams')
        .order_by('user__first_name', 'user__username', 'extension')
    )

    agent_rows = []
    total_calls = 0
    total_answered = 0
    active_count = 0
    on_call_count = 0
    logged_out_count = 0

    for agent in agents:
        live_state = live_agent_states.get(agent.id)
        is_live = live_state is not None
        state = live_state.get('state') if live_state else None
        is_on_call = is_live and bool(live_state.get('current_call_id') or state == 'busy')

        if is_live:
            active_count += 1
        else:
            logged_out_count += 1
        if is_on_call:
            on_call_count += 1

        agent_logs = logs_by_agent.get(agent.id, [])
        agent_summary = _get_agent_summary_data(agent.id, selected_date)
        logged_out_seconds = 0
        if agent_summary:
            logged_out_seconds = int(agent_summary["total_logged_out"].total_seconds())

        stats = call_stats_by_agent.get(agent.id, {})
        agent_total_calls = stats.get('total_calls', 0)
        agent_answered_calls = stats.get('answered_calls', 0)
        total_calls += agent_total_calls
        total_answered += agent_answered_calls
        connect_ratio = round((agent_answered_calls / agent_total_calls) * 100, 1) if agent_total_calls else 0

        first_log = agent_logs[0] if agent_logs else None
        latest_login = latest_login_logs.get(agent.id)
        latest_logout = latest_logout_logs.get(agent.id)

        if is_live:
            status_label = 'On Call' if is_on_call else 'Active'
            status_detail = 'Active since'
            status_time = None
            if latest_login and (not latest_logout or latest_login.created_at >= latest_logout.created_at):
                status_time = latest_login.created_at.astimezone(karachi_tz)
        else:
            status_label = 'Logged Out'
            status_detail = 'Last active at'
            status_time = latest_logout.created_at.astimezone(karachi_tz) if latest_logout else None

        team_names = ', '.join(team.get_name_display() for team in agent.teams.all())
        agent_name = agent.user.get_full_name() or agent.user.username if agent.user else f'Agent {agent.id}'

        agent_rows.append({
            'agent': agent,
            'agent_name': agent_name,
            'extension': agent.extension,
            'teams': team_names or 'No team',
            'status_label': status_label,
            'status_class': 'on-call' if is_on_call else 'active' if is_live else 'logged-out',
            'status_detail': status_detail,
            'status_time': status_time,
            'state_label': (state or 'offline').replace('_', ' ').title(),
            'current_call_id': live_state.get('current_call_id') if live_state else None,
            'total_calls': agent_total_calls,
            'answered_calls': agent_answered_calls,
            'connect_ratio': connect_ratio,
            'logged_out_duration': _format_duration(logged_out_seconds),
            'logged_out_seconds': logged_out_seconds,
            'first_log': first_log,
            'first_log_time': first_log.created_at.astimezone(karachi_tz) if first_log else None,
            'selected_campaign': agent.selected_campaign,
        })

    agent_rows.sort(key=lambda row: (row['status_class'] == 'logged-out', -row['total_calls'], row['agent_name']))

    context = {
        'agent_rows': agent_rows,
        'selected_date': selected_date_value,
        'last_refreshed': now_pk,
        'error_message': error_message,
        'summary': {
            'total_agents': len(agent_rows),
            'active_agents': active_count,
            'on_call_agents': on_call_count,
            'logged_out_agents': logged_out_count,
            'total_calls': total_calls,
            'connect_ratio': round((total_answered / total_calls) * 100, 1) if total_calls else 0,
        },
    }

    return render(request, 'dialer/manager_dashboard.html', context)
    

@csrf_exempt
@require_http_methods(["POST"])
def activate_campaign(request):
    try:
        data = json.loads(request.body)
        campaign_pk = data.get('id')
        agent_id = data.get('agent_id')

        if not campaign_pk or not agent_id:
            return JsonResponse({'success': False, 'message': 'id and agent_id are required'}, status=400)

        try:
            agent = Agent.objects.get(id=agent_id)
        except Agent.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Agent not found'}, status=404)

        try:
            campaign = Campaign.objects.get(id=campaign_pk, agent=agent)
        except Campaign.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Campaign not found'}, status=404)

        Agent.objects.filter(id=agent_id).update(selected_campaign=campaign)

        return JsonResponse({
            'success': True,
            'campaign': {
                'id': campaign.id,
                'campaign_id': campaign.campaign_id,
                'campaign_name': campaign.campaign_name,
                'segment': campaign.segment,
            }
        })

    except Exception as e:
        logger.exception(f"Error activating campaign: {e}")
        return JsonResponse({'success': False, 'message': 'Internal server error'}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def deactivate_campaign(request):
    try:
        data = json.loads(request.body)
        agent_id = data.get('agent_id')

        if not agent_id:
            return JsonResponse({'success': False, 'message': 'agent_id is required'}, status=400)

        updated = Agent.objects.filter(id=agent_id).update(selected_campaign=None)
        if not updated:
            return JsonResponse({'success': False, 'message': 'Agent not found'}, status=404)

        return JsonResponse({'success': True})

    except Exception as e:
        logger.exception(f"Error deactivating campaign: {e}")
        return JsonResponse({'success': False, 'message': 'Internal server error'}, status=500)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def formdata_submission(request):
    data = json.loads(request.body)
    phone_number = data.get('followup_phone_number')
    date_value = data.get('followup_date')
    time_value = data.get('followup_time')
    comment = data.get('followup_comments')

    lead = Lead.objects.filter(phone_number=phone_number).order_by('-created_at').first()
    if not lead:
        logger.error(f'Error in Form data Submission: no lead with phone {phone_number} exists')
        return JsonResponse({'success': False, 'message': 'Lead not found'}, status=404)

    try:
        scheduled_date = datetime.strptime(date_value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return JsonResponse({
            'success': False,
            'message': 'date is required in YYYY-MM-DD format'
        }, status=400)

    karachi_tz = pytz.timezone('Asia/Karachi')
    now_pk = timezone.now().astimezone(karachi_tz)
    today_pk = now_pk.date()

    fire_task = False
    if scheduled_date == today_pk:
        fire_task = True

    if scheduled_date < today_pk:
        return JsonResponse({
            'success': False,
            'message': 'date cannot be in the past'
        }, status=400)

    if time_value:
        try:
            scheduled_time = datetime.strptime(time_value, '%H:%M').time()
        except (TypeError, ValueError):
            return JsonResponse({
                'success': False,
                'message': 'time must be in HH:MM format'
            }, status=400)

        scheduled_pk = karachi_tz.localize(datetime.combine(scheduled_date, scheduled_time))
        if scheduled_date == today_pk and scheduled_pk <= now_pk:
            return JsonResponse({
                'success': False,
                'message': 'time cannot be in the past for today'
            }, status=400)
    else:
        if scheduled_date == today_pk:
            return JsonResponse({
                'success': False,
                'message': 'time is required for current date'
            }, status=400)
        scheduled_pk = None

    lead.follow_up_date = scheduled_date
    lead.follow_up_time = scheduled_pk.time() if scheduled_pk else None
    lead.comment = comment
    lead.save()

    if fire_task:
        scheduled_utc = scheduled_pk.astimezone(pytz.utc)
        formdata_scheduled_task.apply_async(
            args=[lead.id],
            eta=scheduled_utc,
        )


    return JsonResponse({
        'success': True
    })
