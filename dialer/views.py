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
                all_agents.append({
                    "user__username": 'yellow',
                    "extension": '1004'
                })
                
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
    

