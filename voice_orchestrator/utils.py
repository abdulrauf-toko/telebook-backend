import os
from lxml import etree
from django.conf import settings
from threading import Lock
from voice_orchestrator.freeswitch import fs_manager
import boto3
from datetime import date
import logging
import csv
import io
from django.utils import timezone
import datetime
import shutil
import subprocess

_xml_lock = Lock()
logger = logging.getLogger(__name__)

DEFAULT_XML = os.path.join(settings.FS_DIR, "default.xml")

def fs_create_user(extension, password, group=None):
    # 1. Write the user XML file
    user_xml = f"""<include>
      <user id="{extension}">
        <params>
          <param name="password" value="{password}"/>
        </params>
        <variables>
          <variable name="user_context" value="default"/>
          <variable name="effective_caller_id_number" value="{extension}"/>
          <variable name="media_webrtc" value="true"/>
          <variable name="rtp_secure_media" value="mandatory:AES_CM_128_HMAC_SHA1_80"/>
        </variables>
      </user>
    </include>"""

    user_file = os.path.join(settings.FS_DIR, "default", f"{extension}.xml")
    with open(user_file, 'w') as f:
        f.write(user_xml)

    # 2. Add pointer to the group in default.xml if one is provided
    if group:
        fs_add_user_to_group(extension, group)

    # 3. Reload FreeSWITCH
    fs_manager.api('reloadxml')


def fs_add_user_to_group(extension, group_name):
    tree = etree.parse(DEFAULT_XML)
    root = tree.getroot()

    # Find the target group
    groups = root.findall(f".//group[@name='{group_name}']")
    if not groups:
        raise ValueError(f"Group {group_name} not found")

    group = groups[0]
    users_el = group.find('users')

    # Check if pointer already exists
    existing = users_el.findall(f"user[@id='{extension}']")
    if existing:
        return  # already there

    # Add the pointer
    new_user = etree.SubElement(users_el, 'user')
    new_user.set('id', str(extension))
    new_user.set('type', 'pointer')

    # Write back
    tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

def fs_move_user(extension, from_group, to_group):
    with _xml_lock:
        tree = etree.parse(DEFAULT_XML)
        root = tree.getroot()

        # Remove from old group
        old_group = root.find(f".//group[@name='{from_group}']/users")
        for u in old_group.findall(f"user[@id='{extension}']"):
            old_group.remove(u)

        # Add to new group
        new_group = root.find(f".//group[@name='{to_group}']/users")
        new_user = etree.SubElement(new_group, 'user')
        new_user.set('id', str(extension))
        new_user.set('type', 'pointer')

        tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

    fs_manager.api('reloadxml')

def fs_delete_user(extension):
    # Remove user file
    user_file = os.path.join(settings.FS_DIR, "default", f"{extension}.xml")
    if os.path.exists(user_file):
        os.remove(user_file)

    # Remove pointer from all groups
    with _xml_lock:
        tree = etree.parse(DEFAULT_XML)
        root = tree.getroot()
        for user_el in root.findall(f".//user[@id='{extension}'][@type='pointer']"):
            user_el.getparent().remove(user_el)
        tree.write(DEFAULT_XML, pretty_print=True, xml_declaration=True, encoding='UTF-8')

    fs_manager.api('reloadxml')


def upload_call_recording(file_path: str, recording_date: date) -> str:
    aws_access_key = settings.AWS_ACCESS_KEY_ID
    aws_secret_key = settings.AWS_SECRET_ACCESS_KEY
    bucket_name = settings.AWS_STORAGE_BUCKET_NAME
    region_name = settings.AWS_S3_REGION_NAME

    file_name = os.path.basename(file_path)
    s3_key = (
        f"call-recordings/"
        f"{recording_date.year}/"
        f"{recording_date.month:02d}/"
        f"{recording_date.day:02d}/"
        f"{file_name}"
    )

    s3_client = boto3.client(
        's3',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=region_name,
    )

    s3_client.upload_file(
        Filename=file_path,
        Bucket=bucket_name,
        Key=s3_key,
    )

    url = f"https://{bucket_name}.s3.{region_name}.amazonaws.com/{s3_key}"
    return url

def upload_call_logs(file_path: str, today: date) -> str:

    s3_key = f"call-logs/call_logs_{today.strftime('%Y-%m-%d')}.csv"

    s3_client = boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
    )

    s3_client.upload_file(
        Filename=file_path,
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Key=s3_key,
    )    
    url = f"https://{settings.AWS_STORAGE_BUCKET_NAME}.s3.{settings.AWS_S3_REGION_NAME}.amazonaws.com/{s3_key}"
    return url


def export_today_call_logs_to_csv(start_date: date, end_date: date) -> str:
    from dialer.models import CallLog
    try:
        # Query all call logs for today
        start_dt = timezone.make_aware(datetime.datetime.combine(start_date, datetime.time.min))
        end_dt   = timezone.make_aware(datetime.datetime.combine(end_date,   datetime.time.max))

        all_call_logs = CallLog.objects.filter(
            initiated_at__gte=start_dt,
            initiated_at__lte=end_dt,
        ).select_related('agent', 'agent__user', 'lead', 'campaign').order_by('to_number', '-initiated_at')
        
        # Group by phone number and keep only the latest call
        latest_calls_dict = {}
        for call_log in all_call_logs:
            if call_log.lead:
                phone_number = call_log.lead.phone_number
                if phone_number not in latest_calls_dict:
                    latest_calls_dict[phone_number] = call_log
            else:
                phone_number = call_log.to_number
                if phone_number.startswith("03") and phone_number not in latest_calls_dict:
                    latest_calls_dict[phone_number] = call_log

        
        call_logs = latest_calls_dict.values()
        
        # Define CSV headers
        fieldnames = [
            'call_id',
            'callType',
            'src',
            'billsec',
            'end',
            'dukaan_account_id',
            'formData',
            'uniqueid',
            'number',
            'lead_id',
            'disposition',
            'agentName',
            'duration',
            'dialTime',
            'segment',
            'name',
            "link"
        ]
        
        # Set default output path if not provided
        output_path = f"/home/pbx/call_logs_{start_date.strftime('%Y-%m-%d')}-{end_date.strftime('%Y-%m-%d')}.csv"
        
        # Create and write CSV file
        with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            # Write each call log as a row
            for call_log in call_logs:
                # Get agent details
                agent_name = None
                agent_username = None
                if call_log.agent and call_log.agent.telecard_username:
                    agent_name = call_log.agent.user.get_full_name()
                    agent_username = call_log.agent.telecard_username
                
                # Get campaign segment
                segment = None  # default segment
                lead_id = None
                to_number = None
                if call_log.lead:
                    lead_id = call_log.lead.udhaar_lead_id
                    to_number = call_log.lead.phone_number
                    # dukaan_account_id = call_log.lead.dukaan_account_id
                    segment = "everyday-campaign"  # default segment for all leads
                else:
                    segment = 'rupin-emi-campaign'
                    to_number = call_log.to_number
                
                # Format timestamps
                dial_time = call_log.initiated_at.strftime('%Y-%m-%d %H:%M:%S') if call_log.initiated_at else None
                end_time = call_log.ended_at.strftime('%Y-%m-%d %H:%M:%S') if call_log.ended_at else None
                
                # Map disposition (convert Django status to expected format)
                disposition_map = {
                    'answered': 'ANSWERED',
                    'failed': 'FAILED',
                    'no_answer': 'NO ANSWER',
                    'busy': 'BUSY',
                    'lose_race': 'BUSY',
                    'user_not_registered': 'FAILED',
                    'invalid': 'FAILED',
                    'cancelled': 'FAILED',
                }
                disposition = disposition_map.get(call_log.status, call_log.status.upper())

                recording_url = call_log.recording_url if disposition == 'ANSWERED' else None
                if recording_url and not recording_url.startswith(("http://", "https://")):
                    recording_url = None  # Only include valid URLs
                
                # Create row
                row = {
                    'call_id': call_log.call_id,
                    'callType': call_log.call_direction.title() or '',
                    'src': "2138722005",
                    'billsec': call_log.talk_time_seconds,
                    'end': end_time,
                    'dukaan_account_id': None,
                    'formData': None,
                    'uniqueid': call_log.call_id,
                    'number': to_number,
                    'lead_id': lead_id,
                    'disposition': disposition,
                    'agentName': agent_username,
                    'duration': call_log.duration_seconds,
                    'dialTime': dial_time,
                    'segment': segment,
                    'name': agent_name,
                    'link': recording_url,
                }
                
                writer.writerow(row)
        
        logger.info(f"Successfully exported {len(latest_calls_dict)} unique phone numbers (call logs) to {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error exporting call logs to CSV: {e}")
        raise


def convert_wav_to_mp3(file_path: str) -> str:
    if not file_path:
        return None 
    ffmpeg_path = (
        shutil.which("ffmpeg")
    )

    # output_path = os.path.splitext(file_path)[0] + ".mp3"
    # os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    file_name = os.path.splitext(os.path.basename(file_path))[0]
    output_path = f"/tmp/{file_name}.mp3"

    command = [
        ffmpeg_path,
        "-y",
        "-i",
        file_path,
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        output_path,
    ]

    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to convert wav to mp3: %s", exc.stderr)
        raise RuntimeError(f"ffmpeg conversion failed: {exc.stderr.strip()}") from exc

    # os.remove(file_path)
    return output_path
