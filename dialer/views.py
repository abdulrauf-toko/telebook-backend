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
from django.shortcuts import get_object_or_404, render
from django.db.models import Q
import orjson as json
import csv
import io
import logging
import re
from datetime import date
import pytz
from events.utils import mark_agent_idle_in_cache, mark_agent_logged_in_cache, logout_agent

from django.contrib.auth import authenticate, login, logout
from .models import Agent, Campaign, Lead, CallLog

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


def agent_dashboard(request):
    """
    Dashboard view for agents to see their call logs and leads for today.
    Shows call logs for today, total call logs, and total leads assigned.
    """
    username = request.GET.get('username') or request.POST.get('username')
    agent = None
    call_logs = []
    unique_phone_count = 0
    today_leads_count = 0
    error_message = None
    stats = {}
    
    if username:
        try:
            from django.contrib.auth.models import User
            user = User.objects.get(username=username)
            agent = Agent.objects.get(user=user)
            
            # Get today's date
            today = date.today()
            
            # Get all call logs for today for this agent
            call_logs = CallLog.objects.filter(
                agent=agent,
                initiated_at__date=today
            ).select_related('lead', 'campaign').order_by('-initiated_at')
            
            # Convert call logs to Karachi timezone
            karachi_tz = pytz.timezone('Asia/Karachi')
            for log in call_logs:
                if log.initiated_at:
                    log.initiated_at_karachi = log.initiated_at.astimezone(karachi_tz)
                else:
                    log.initiated_at_karachi = None
            
            # Count unique phone numbers called today
            unique_phone_count = call_logs.values('to_number').distinct().count()
            
            # Get leads assigned to this agent today
            today_leads = Lead.objects.filter(
                campaign__active=True,
                campaign__created_at__date=today,
                campaign__agent=agent
            )
            today_leads_count = today_leads.count()
            
            
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
    }
    
    return render(request, 'dialer/dashboard.html', context)
    

