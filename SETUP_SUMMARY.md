# VoIP Backend Orchestrator - Core Setup Summary

## Completed: Phase 1 - Settings & Models

### 1. Settings Configuration (`voice_orchestrator/settings.py`)

#### Database Configuration
- **Engine**: PostgreSQL (configurable via environment variables)
- **Environment Variables**:
  - `DB_NAME`: Database name (default: `voice_orchestrator`)
  - `DB_USER`: Database user (default: `postgres`)
  - `DB_PASSWORD`: Database password (default: `postgres`)
  - `DB_HOST`: Database host (default: `localhost`)
  - `DB_PORT`: Database port (default: `5432`)

#### Celery Configuration
- **Broker**: Redis (default: `redis://localhost:6379/0`)
- **Result Backend**: Redis (default: `redis://localhost:6379/0`)
- **Serialization**: JSON (for payload compatibility)
- **Task Tracking**: Enabled with 30-minute hard limit

#### Queue Configuration
Two queues implemented as specified:

1. **default** (Parallel Queue)
   - For heavy processing: logging, analytics, data enrichment
   - Exchange: `default`, Routing Key: `default`
   - No concurrency limit - scales horizontally

2. **dialer_queue** (Sequential Queue)
   - For dialer logic only to prevent race conditions
   - Exchange: `dialer`, Routing Key: `dialer.sequential`
   - **MUST be run with `celery -A voice_orchestrator worker -Q dialer_queue -c 1`**

#### Routing Configuration
Tasks automatically routed:
- `dialer.tasks.process_dialer_logic` → `dialer_queue`
- `dialer.tasks.execute_dial` → `dialer_queue`
- `dialer.tasks.handle_agent_state` → `dialer_queue`

#### Caching Configuration
- **Backend**: Django-Redis (using Redis)
- **Cache Location**: `redis://localhost:6379/1` (separate DB from broker)
- **Default Timeout**: 300 seconds (5 minutes)

**Agent State Caching**:
- Key Prefix: `agent_state:`
- Timeout: 600 seconds (10 minutes)
- Format: `agent_state:{agent_id}` → `{"status": "available", ...}`

**Call State Caching**:
- Key Prefix: `call_state:`
- Timeout: 3600 seconds (1 hour)

#### Logging Configuration
- **Format**: Verbose with timestamp, module, process/thread ID
- **Log File**: `logs/voice_orchestrator.log` (auto-created)
- **Rotation**: 10MB per file, 5 backups retained
- **Loggers**: Django, dialer, events apps configured

#### FreeSWITCH Configuration
- **ESL Host**: `localhost` (configurable via `FREESWITCH_ESL_HOST`)
- **ESL Port**: `8021` (configurable via `FREESWITCH_ESL_PORT`)
- **ESL Password**: `ClueCon` (configurable via `FREESWITCH_ESL_PASSWORD`)
- **Event Channel**: `freeswitch:events` (Redis pub/sub channel)

---

### 2. Dialer App Models (`dialer/models.py`)

#### Agent Model
Represents call center agents with comprehensive metadata:
- **Fields**:
  - `agent_id`: Unique identifier (e.g., `agent_101`)
  - `extension`: SIP/VoIP extension
  - `phone_number`: Agent's contact number
  - `status`: Real-time status (cached in Redis)
  - `is_active`: Enable/disable agent
  - `max_concurrent_calls`: Concurrency limit (1-10)
  - `user`: Optional Django User association

- **Status Values**: available, busy, break, offline, on_call
- **Indexes**: agent_id, status, is_active

#### DialerCampaign Model
Manages outbound dialing campaigns:
- **Fields**:
  - `campaign_id`: UUID-based unique identifier
  - `name`, `description`: Campaign metadata
  - `status`: draft, scheduled, active, paused, completed, cancelled
  - `assigned_agents`: ManyToMany relationship with Agent
  - `target_list`: ForeignKey to contact list
  - `calls_per_agent_per_hour`: Rate limiting
  - `max_retry_attempts`: Retry configuration
  - `retry_delay_minutes`: Time between retries
  - `scheduled_start/end`: Campaign timing
  - `created_by`: User who created campaign

- **Indexes**: campaign_id, status, created_at

#### DialerTargetList Model
Manages contact lists for campaigns:
- **Fields**:
  - `target_list_id`: UUID-based unique identifier
  - `total_contacts`: Total contact count
  - `processed_contacts`: Successfully processed count
  - Tracks import source metadata

#### CallLog Model
Comprehensive call record tracking:
- **Fields**:
  - `call_id`: UUID-based unique identifier
  - `freeswitch_uuid`: FreeSWITCH channel UUID
  - `from_extension`: Originating agent extension
  - `to_number`: Destination phone number
  - `agent`: ForeignKey to Agent
  - `campaign`: ForeignKey to DialerCampaign
  - `status`: initiated, dialing, answered, completed, failed, no_answer, busy, invalid, cancelled
  - `disconnect_reason`: Hangup cause from FreeSWITCH
  - `initiated_at`, `answered_at`, `ended_at`: Precise timing
  - `duration_seconds`, `talk_time_seconds`: Call metrics
  - `attempt_number`, `retry_after`: Retry tracking
  - `recording_url`, `recording_stored`: Recording metadata
  - `cdr_data`: Full CDR from FreeSWITCH (JSON)

- **Indexes**: call_id, freeswitch_uuid, agent+timestamp, campaign+status, status+timestamp, to_number

#### AgentState Model
Historical tracking of agent state changes:
- **Fields**:
  - `agent`: ForeignKey to Agent
  - `state`: Current state (available, busy, break, offline, on_call)
  - `timestamp`: When state changed
  - `call`: Associated CallLog (if applicable)
  - `reason`: Why state changed
  - `metadata`: Additional context (JSON)

- **Indexes**: agent+timestamp, timestamp
- **Purpose**: Audit trail for compliance and debugging (real-time state in Redis)

---

### 3. Events App Models (`events/models.py`)

#### FreeSwitchEvent Model
Raw event log from FreeSWITCH ESL:
- **Fields**:
  - `event_id`: UUID-based unique identifier
  - `event_type`: Classification (CHANNEL_ANSWER, CHANNEL_HANGUP, etc.)
  - `channel_uuid`: FreeSWITCH channel UUID
  - `channel_name`: Channel identifier
  - `caller_id_number`, `caller_id_name`: ANI information
  - `destination_number`: Called number
  - `timestamp`: When received by system
  - `freeswitch_timestamp`: FreeSWITCH server timestamp
  - `event_data`: Full event payload (JSON)
  - `hangup_cause`: Disconnect reason
  - `call_duration_ms`, `answer_epoch`, `hangup_epoch`: Timing data

- **Indexes**: event_type+timestamp, channel_uuid, destination_number+timestamp, timestamp
- **Purpose**: Complete audit trail and event replay capability

#### EventProcessingStatus Model
Tracks event processing state:
- **Fields**:
  - `event`: OneToOne reference to FreeSwitchEvent
  - `status`: pending, processing, processed, failed, skipped
  - `attempts`: Number of retry attempts
  - `last_attempt_at`, `next_retry_at`: Scheduling
  - `last_error`, `error_traceback`: Error details
  - `processed_at`: Completion time
  - `call_log_id`: Associated CallLog

- **Indexes**: status, status+next_retry_at
- **Purpose**: Reliable event processing with retry logic

#### CallStateEvent Model
Simplified call state transition tracking:
- **Fields**:
  - `freeswitch_event`: Reference to FreeSwitchEvent
  - `channel_uuid`: FreeSWITCH channel UUID
  - `call_log_id`: Associated CallLog
  - `previous_state`, `current_state`: State transition
  - `state_duration_ms`: Time in previous state
  - `timestamp`: Transition time
  - `metadata`: Additional context

- **States**: initiated, alerting, answered, bridged, held, ended
- **Indexes**: channel_uuid+timestamp, call_log_id+timestamp, current_state
- **Purpose**: Simplified view for dialer logic

#### EventBatch Model
Batch processing for efficiency:
- **Fields**:
  - `batch_id`: UUID-based unique identifier
  - `status`: created, processing, completed, failed
  - `events`: ManyToMany with FreeSwitchEvent
  - `event_count`: Total in batch
  - `processed_count`: Successfully processed
  - `created_at`, `started_at`, `completed_at`: Timing
  - `error_message`: Failure details

- **Indexes**: batch_id, status
- **Purpose**: Reduce database load with batch processing

---

## Next Steps

1. **Install Dependencies**:
   ```bash
   pip install django-redis celery redis psycopg2-binary
   ```

2. **Create Migrations**:
   ```bash
   python manage.py makemigrations dialer events
   python manage.py migrate
   ```

3. **Create Admin Interfaces**:
   - Configure `dialer/admin.py` with ModelAdmin classes
   - Configure `events/admin.py` with ModelAdmin classes

4. **Implement Celery Tasks**:
   - `dialer/tasks.py`: Dialer logic and sequential processing
   - `events/tasks.py`: Event processing and state management

5. **Implement Event Handlers**:
   - ESL event listener (connect to FreeSWITCH)
   - Redis pub/sub subscriber
   - Event parsing and dispatch

6. **Implement Dialer Logic**:
   - Agent state machine
   - Call rate limiting
   - Retry logic
   - Sequential dial processing

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  FreeSWITCH (VoIP Server)                                   │
└────────────────┬────────────────────────────────────────────┘
                 │ ESL Events
                 ▼
┌──────────────────────────────────────────────────────────────┐
│  ESL Dispatcher (External)                                   │
│  └─ Pushes events to Redis: freeswitch:events                │
└────────────────┬─────────────────────────────────────────────┘
                 │
         ┌───────┴──────────┐
         ▼                  ▼
    ┌─────────────┐   ┌──────────────┐
    │   Redis     │   │ PostgreSQL   │
    │  (Broker)   │   │  (Database)  │
    │  (Cache)    │   │              │
    └─────────────┘   └──────────────┘
         ▲                  ▲
         │                  │
    ┌────┴──────────┬───────┴────┐
    │               │            │
    ▼               ▼            ▼
┌───────────┐ ┌─────────────┐ ┌──────────────┐
│ Celery    │ │   Django    │ │   Django     │
│ Broker    │ │   App       │ │   App        │
└───────────┘ └─────────────┘ └──────────────┘
    ▲
    ├─ Worker 1: default queue (parallel)
    ├─ Worker 2: dialer_queue (sequential, concurrency=1)
```

---

## Configuration Checklist

- [x] Settings configured with PostgreSQL
- [x] Celery broker and result backend configured
- [x] Two-queue system (default + dialer_queue)
- [x] Redis caching for agent/call states
- [x] Task routing configured
- [x] Dialer models with all required fields
- [x] Event models for FreeSWITCH tracking
- [x] Agent state history tracking
- [x] Call log with CDR storage
- [x] Logging configured
- [ ] Migrations created
- [ ] Admin interfaces configured
- [ ] Tasks implemented
- [ ] Event handlers implemented
- [ ] Dialer logic implemented

