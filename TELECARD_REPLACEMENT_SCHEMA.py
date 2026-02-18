"""
Comprehensive documentation for the Telecard Replacement Database Schema.

This document outlines the new database schema that replaces Telecard functionality
in the Voice Orchestrator project.
"""

# DATABASE SCHEMA OVERVIEW
# =======================
#
# The new system replaces Telecard with the following Django models:
#
# 1. Campaign (replaces Telecard campaign management)
#    - Stores campaign metadata and status
#    - Associates leads with agents and campaign types
#    - Tracks call statistics and completion rates
#
# 2. Lead (replaces Telecard call queue items)
#    - Stores customer/lead information from Udhaar
#    - Tracks lead status, attempts, and segments
#    - Maintains connection to campaigns
#
# 3. LeadImportLog (replaces Telecard import tracking)
#    - Audits all CSV uploads and data imports
#    - Tracks processing status and errors
#    - Maintains import statistics
#
# 4. Extended CallLog
#    - Now references both Campaign and Lead
#    - Enhanced with call recording and CDR data
#
# 5. Agent (already exists)
#    - Enhanced with status tracking
#    - Manages agent assignments to campaigns
#
# DATABASE TABLES
# ===============
#
# dialer_campaign
#   ├─ id (PK)
#   ├─ campaign_id (UNIQUE, indexed) - "AgentAhmed-Follow_Up-20260209"
#   ├─ campaign_name
#   ├─ segment (FK) - follow_up, active, growth, active_churn, growth_churn, acquisition
#   ├─ agent_id (FK)
#   ├─ status (indexed) - draft, active, paused, completed, archived
#   ├─ duration_days
#   ├─ created_at (indexed)
#   ├─ started_at
#   ├─ completed_at
#   ├─ updated_at
#   ├─ total_leads
#   ├─ called_leads
#   ├─ completed_calls
#   ├─ failed_calls
#   ├─ no_answer_calls
#   └─ metadata (JSON)
#
# dialer_lead
#   ├─ id (PK)
#   ├─ lead_id (UNIQUE, indexed)
#   ├─ phone_number (indexed, normalized e.g., "923001234567")
#   ├─ customer_name
#   ├─ city
#   ├─ segment (indexed, same as campaign)
#   ├─ campaign_id (FK)
#   ├─ customer_segment - micro/small/medium/large
#   ├─ last_order_date
#   ├─ last_call_date
#   ├─ follow_up_date
#   ├─ status (indexed) - pending, called, skipped, completed, failed, do_not_call
#   ├─ attempt_count
#   ├─ max_attempts
#   ├─ metadata (JSON)
#   ├─ created_at (indexed)
#   └─ updated_at
#
# dialer_leadimportlog
#   ├─ id (PK)
#   ├─ import_id (UNIQUE, indexed)
#   ├─ filename
#   ├─ file_size_bytes
#   ├─ campaign_id (FK)
#   ├─ status (indexed) - pending, processing, completed, failed, partial
#   ├─ started_at
#   ├─ completed_at
#   ├─ total_rows
#   ├─ successful_imports
#   ├─ failed_imports
#   ├─ skipped_rows
#   ├─ error_log (JSON)
#   ├─ summary
#   └─ created_at (indexed)
#
# dialer_calllog (extended)
#   ├─ id (PK)
#   ├─ call_id (UNIQUE, indexed)
#   ├─ freeswitch_uuid (indexed)
#   ├─ agent_id (FK)
#   ├─ lead_id (FK) - NEW
#   ├─ campaign_id (FK) - NEW
#   ├─ from_extension
#   ├─ to_number (indexed)
#   ├─ status (indexed)
#   ├─ disconnect_reason
#   ├─ initiated_at (indexed)
#   ├─ answered_at
#   ├─ ended_at
#   ├─ duration_seconds
#   ├─ talk_time_seconds
#   ├─ attempt_number
#   ├─ retry_after
#   ├─ recording_url
#   ├─ recording_stored
#   └─ cdr_data (JSON)
#
#
# API ENDPOINTS
# =============
#
# Agents Management:
#   GET    /api/dialer/agents/                    - List all agents
#   POST   /api/dialer/agents/                    - Create new agent
#   GET    /api/dialer/agents/{id}/               - Get agent details
#   PUT    /api/dialer/agents/{id}/               - Update agent
#   DELETE /api/dialer/agents/{id}/               - Delete agent
#
# Campaign Management:
#   GET    /api/dialer/campaigns/                 - List all campaigns
#   POST   /api/dialer/campaigns/                 - Create new campaign
#   GET    /api/dialer/campaigns/{id}/            - Get campaign details
#   PUT    /api/dialer/campaigns/{id}/            - Update campaign
#   DELETE /api/dialer/campaigns/{id}/            - Delete campaign
#   POST   /api/dialer/campaigns/{id}/activate/   - Activate campaign
#   POST   /api/dialer/campaigns/{id}/pause/      - Pause campaign
#   POST   /api/dialer/campaigns/{id}/complete/   - Mark as completed
#   GET    /api/dialer/campaigns/{id}/stats/      - Get campaign statistics
#
# Lead Management:
#   GET    /api/dialer/leads/                     - List all leads
#   GET    /api/dialer/leads/{id}/                - Get lead details
#   PUT    /api/dialer/leads/{id}/                - Update lead
#   POST   /api/dialer/leads/{id}/mark_as_called/ - Mark as called
#   POST   /api/dialer/leads/{id}/mark_as_completed/ - Mark as completed
#
# CSV Upload (Telecard Replacement):
#   POST   /api/dialer/csv-upload/upload/         - Upload CSV file with leads
#                                                   (replaces telecard.assign_numbers_to_campaign)
#
# Import Logs:
#   GET    /api/dialer/import-logs/               - List all import logs
#   GET    /api/dialer/import-logs/{id}/          - Get import log details
#
# Call Logs:
#   GET    /api/dialer/call-logs/                 - List all call logs
#   GET    /api/dialer/call-logs/{id}/            - Get call log details
#
# Agent State History:
#   GET    /api/dialer/agent-states/              - List agent state changes
#   GET    /api/dialer/agent-states/{id}/         - Get state change details
#
#
# TELECARD REPLACEMENT MAPPING
# =============================
#
# OLD TELECARD OPERATION           NEW VOICE ORCHESTRATOR OPERATION
#
# 1. Create Campaign:
#    telecard.create_campaign()      → POST /api/dialer/campaigns/
#    PAYLOAD:                           PAYLOAD:
#    {                                  {
#      "campaign_name": "...",            "campaign_name": "AgentAhmed-Follow_Up",
#      "status": 1,                       "segment": "follow_up",
#      "days": 1                          "agent": 1,
#    }                                    "duration_days": 1
#                                       }
#    RESPONSE:                          RESPONSE:
#    {                                  {
#      "campaignId": 98761,               "id": 1,
#      "status": "success"                "campaign_id": "AgentAhmed-Follow_Up-20260209",
#    }                                    "status": "draft"
#                                       }
#
# 2. Assign Numbers to Campaign:
#    telecard.assign_numbers_        → POST /api/dialer/csv-upload/upload/
#    to_campaign()
#
#    PAYLOAD:                           PAYLOAD (multipart/form-data):
#    {                                  {
#      "campaign_id": 98761,              "campaign_id": "AgentAhmed-Follow_Up-20260209",
#      "filename": "leads.csv"            "segment": "follow_up",
#    }                                    "csv_file": <file>
#    CSV File:                          CSV File:
#    name,number,city,attempts           name,number,city,attempts
#    Ahmed Khan,923001234567,...         Ahmed Khan,923001234567,Karachi,1
#
#    RESPONSE:                          RESPONSE:
#    {                                  {
#      "numbersAssigned": 14,             "import_id": "550e8400-e29b-41d4-a716-...",
#      "status": "success"                "campaign_id": "AgentAhmed-Follow_Up-20260209",
#    }                                    "successful_imports": 14,
#                                        "failed_imports": 0,
#                                        "status": "completed"
#                                       }
#
# 3. Assign Agent to Campaign:
#    telecard.assign_agent_into_     → POST /api/dialer/campaigns/{id}/activate/
#    campaign()
#
#    PAYLOAD:                           PAYLOAD (if not already assigned):
#    {                                  {
#      "campaign_id": 98761,              (agent assigned during creation)
#      "users": ["AgentAhmed"]           }
#    }
#                                       Then activate:
#    RESPONSE:                          POST /api/dialer/campaigns/{id}/activate/
#    {
#      "agentAssigned": "AgentAhmed",    RESPONSE:
#      "status": "success"               {
#    }                                     "message": "Campaign activated",
#                                         "campaign": {...}
#                                       }
#
#
# MIGRATION STEPS
# ===============
#
# 1. Run Django migrations:
#    $ python manage.py makemigrations
#    $ python manage.py migrate
#
# 2. Create agents in new system:
#    POST /api/dialer/agents/ with agent data
#
# 3. For each campaign in Udhaar:
#    a) Create campaign: POST /api/dialer/campaigns/
#    b) Upload leads: POST /api/dialer/csv-upload/upload/
#    c) Activate: POST /api/dialer/campaigns/{id}/activate/
#
# 4. System will automatically handle:
#    - Lead normalization
#    - Duplicate detection
#    - Call queuing
#    - Agent assignment
#
#
# QUERY EXAMPLES
# ==============
#
# Get all active campaigns:
#   GET /api/dialer/campaigns/?status=active
#
# Get leads for a specific campaign:
#   GET /api/dialer/leads/?campaign={campaign_id}
#
# Get pending leads:
#   GET /api/dialer/leads/?status=pending
#
# Filter by segment:
#   GET /api/dialer/leads/?segment=follow_up
#
# Get campaign statistics:
#   GET /api/dialer/campaigns/{id}/stats/
#
# Get import logs for a campaign:
#   GET /api/dialer/import-logs/?campaign={campaign_id}
#
# Get all calls for an agent:
#   GET /api/dialer/call-logs/?agent={agent_id}
#

print(__doc__)
