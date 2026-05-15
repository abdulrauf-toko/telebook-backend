#!/usr/bin/env python3
"""
Export call recordings on a specific date, organised by agent folder.

Usage:
    # All agents in a team
    python export_team_recordings.py <date> --team <team_name>

    # Single agent by Django username
    python export_team_recordings.py <date> --username <username>

Examples:
    python export_team_recordings.py 2026-05-07 --team saddar_growth
    python export_team_recordings.py 2026-05-07 --username john_doe

Recordings (answered calls only) are saved to:
    /home/pbx/recordings_export/<date>/<agent_name>/
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, time
from urllib.parse import urlparse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "voice_orchestrator.settings")

import django

django.setup()

import boto3
from django.conf import settings
from django.utils import timezone

from dialer.models import Agent, CallLog, Team


RECORDINGS_ROOT = "/home/pbx/telebook-pbx/recordings"
EXPORT_ROOT = "/home/pbx/recordings_export"


def _sanitize(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = re.sub(r"\s+", "_", name)
    return name or "unknown"


def _agent_folder_name(agent: Agent) -> str:
    if agent.user:
        full_name = agent.user.get_full_name().strip()
        if full_name:
            return _sanitize(full_name)
        return _sanitize(agent.user.username)
    return _sanitize(f"ext_{agent.extension}")


def _resolve_local_path(recording_value: str) -> str | None:
    if not recording_value or recording_value.startswith(("http://", "https://")):
        return None
    if os.path.isabs(recording_value):
        return recording_value
    return os.path.join(RECORDINGS_ROOT, recording_value.lstrip("/"))


def _parse_s3_url(url: str) -> tuple[str, str]:
    """Return (bucket, key) from a virtual-hosted S3 URL."""
    parsed = urlparse(url)
    # netloc: {bucket}.s3.{region}.amazonaws.com
    bucket = parsed.netloc.split(".s3.")[0]
    key = parsed.path.lstrip("/")
    return bucket, key


def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )


def _download(url: str, dest_path: str) -> bool:
    try:
        bucket, key = _parse_s3_url(url)
        _s3_client().download_file(bucket, key, dest_path)
        return True
    except Exception as exc:
        print(f"    ERROR downloading {url}: {exc}")
        return False


def _copy(src_path: str, dest_path: str) -> bool:
    try:
        shutil.copy2(src_path, dest_path)
        return True
    except Exception as exc:
        print(f"    ERROR copying {src_path}: {exc}")
        return False


def _export_agents(agents, date_str: str, start_dt, end_dt) -> None:
    total_downloaded = 0
    total_skipped = 0
    total_errors = 0

    for agent in agents:
        call_logs = (
            CallLog.objects.filter(
                agent=agent,
                status="answered",
                initiated_at__gte=start_dt,
                initiated_at__lte=end_dt,
            )
            .exclude(recording_url__isnull=True)
            .exclude(recording_url="")
        )

        count = call_logs.count()
        if count == 0:
            continue

        folder_name = _agent_folder_name(agent)
        dest_dir = os.path.join(EXPORT_ROOT, date_str, folder_name)
        os.makedirs(dest_dir, exist_ok=True)

        print(f"\n  {folder_name}  ({count} recording{'s' if count != 1 else ''})")

        for log in call_logs.iterator():
            recording_url = log.recording_url
            original_filename = os.path.basename(recording_url.split("?")[0]) or f"{log.call_id}.wav"
            phone = re.sub(r"[^\w\-]", "", log.to_number or "unknown")
            if log.lead:
                stage = log.lead.emi_stage or "unknown"
            else:
                stage = "unknown"
            dest_filename = f"{folder_name}_{stage}_{phone}_{original_filename}"
            dest_path = os.path.join(dest_dir, dest_filename)

            if os.path.exists(dest_path):
                print(f"    SKIP  {dest_filename}  (already exists)")
                total_skipped += 1
                continue

            if recording_url.startswith(("http://", "https://")):
                print(f"    DL    {dest_filename}")
                ok = _download(recording_url, dest_path)
            else:
                ok = False

            if ok:
                total_downloaded += 1
            else:
                total_errors += 1

    print(f"\n--- Done ---")
    print(f"  Saved:   {total_downloaded}")
    print(f"  Skipped: {total_skipped}")
    print(f"  Errors:  {total_errors}")
    if total_downloaded > 0:
        print(f"  Output:  {os.path.join(EXPORT_ROOT, date_str)}/")


def export_recordings(date_str: str, team_name: str | None = None, username: str | None = None) -> None:
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: Invalid date '{date_str}'. Expected YYYY-MM-DD.")
        sys.exit(1)

    start_dt = timezone.make_aware(datetime.combine(target_date, time.min))
    end_dt = timezone.make_aware(datetime.combine(target_date, time.max))

    if username:
        try:
            agent = Agent.objects.select_related("user").get(user__username=username)
        except Agent.DoesNotExist:
            print(f"ERROR: No agent found with username '{username}'.")
            sys.exit(1)
        print(f"Username: {username}  |  Date: {date_str}")
        _export_agents([agent], date_str, start_dt, end_dt)
    else:
        try:
            team = Team.objects.get(name=team_name)
        except Team.DoesNotExist:
            valid = [c[0] for c in Team.NAME_CHOICES]
            print(f"ERROR: Team '{team_name}' not found. Valid names: {', '.join(valid)}")
            sys.exit(1)

        agents = team.agents.select_related("user").all()
        if not agents.exists():
            print(f"No agents found for team '{team_name}'.")
            return

        print(f"Team: {team_name}  |  Date: {date_str}  |  Agents: {agents.count()}")
        _export_agents(agents, date_str, start_dt, end_dt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export call recordings by team or agent.")
    parser.add_argument("date", help="Date in YYYY-MM-DD format")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--team", metavar="TEAM_NAME", help="Team name (e.g. saddar_growth)")
    group.add_argument("--username", metavar="USERNAME", help="Django username of a specific agent")

    args = parser.parse_args()
    export_recordings(args.date, team_name=args.team, username=args.username)
