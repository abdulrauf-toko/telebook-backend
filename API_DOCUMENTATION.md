# Telecard Replacement: Voice Orchestrator Database & API

## Overview

This document describes the new database schema and REST API that replaces Telecard in the Voice Orchestrator project. The system handles campaign management, lead ingestion from CSV files, and call tracking.

---

## Database Schema

### Models Overview

#### 1. **Campaign** (Replaces Telecard Campaign Management)

Represents a calling campaign assigned to an agent.

```python
Campaign {
    campaign_id: str (unique) # "AgentAhmed-Follow_Up-20260209"
    campaign_name: str
    segment: str # follow_up, active, growth, active_churn, growth_churn, acquisition
    agent: ForeignKey(Agent)
    status: str # draft, active, paused, completed, archived
    duration_days: int
    created_at: datetime
    started_at: datetime (nullable)
    completed_at: datetime (nullable)
    updated_at: datetime
    total_leads: int # Total leads in campaign
    called_leads: int # Leads that have been dialed
    completed_calls: int # Successful calls
    failed_calls: int # Failed attempts
    no_answer_calls: int # No answer responses
    metadata: JSON # Additional context
}
```

**Key Indexes:**
- `(campaign_id)` - Primary lookup
- `(agent, status)` - Filter by agent and status
- `(segment, status)` - Filter by segment
- `(status, created_at)` - Latest campaigns

---

#### 2. **Lead** (Replaces Telecard Call Queue Items)

Represents a customer/lead to be called, imported from Udhaar.

```python
Lead {
    lead_id: str (unique) # "AgentAhmed-Follow_Up-20260209-923001234567"
    phone_number: str (indexed) # Normalized: "923001234567"
    customer_name: str
    city: str
    segment: str # Matches campaign segment
    campaign: ForeignKey(Campaign)
    customer_segment: str # micro/small/medium/large
    last_order_date: datetime (nullable)
    last_call_date: datetime (nullable)
    follow_up_date: datetime (nullable)
    status: str # pending, called, skipped, completed, failed, do_not_call
    attempt_count: int # Number of call attempts
    max_attempts: int # Maximum allowed attempts
    metadata: JSON # order count, feedback status, etc.
    created_at: datetime
    updated_at: datetime
}
```

**Key Indexes:**
- `(phone_number)` - Duplicate detection
- `(campaign, status)` - Get leads for campaign
- `(segment, status)` - Filter by segment
- `(status)` - Get pending leads

**Constraints:**
- `unique_together: [('campaign', 'phone_number')]` - No duplicate phone numbers per campaign

---

#### 3. **LeadImportLog** (Audit Trail for CSV Uploads)

Tracks all CSV uploads and data import operations.

```python
LeadImportLog {
    import_id: str (unique) # UUID
    filename: str # Original CSV filename
    file_size_bytes: int
    campaign: ForeignKey(Campaign)
    status: str # pending, processing, completed, failed, partial
    started_at: datetime (nullable)
    completed_at: datetime (nullable)
    total_rows: int # Total rows processed
    successful_imports: int # Successfully imported
    failed_imports: int # Failed to import
    skipped_rows: int # Duplicates/skipped
    error_log: JSON # List of errors with row numbers
    summary: str # Human-readable summary
    created_at: datetime
}
```

---

#### 4. **CallLog** (Extended to Reference Campaign & Lead)

Existing model enhanced with new foreign keys.

```python
CallLog {
    # ... existing fields ...
    lead: ForeignKey(Lead, nullable) # NEW: Which lead is being called
    campaign: ForeignKey(Campaign, nullable) # NEW: Which campaign
    # ... rest of fields ...
}
```

---

## API Endpoints

### Base URL
```
/api/dialer/
```

### 1. Agents Management

#### List Agents
```
GET /api/dialer/agents/
```

Query Parameters:
- `is_active` - Filter by active status
- `status` - Filter by current status
- `search` - Search by agent_id, extension, phone_number, email
- `ordering` - Sort by created_at or agent_id

**Response:**
```json
{
  "count": 5,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "agent_id": "AgentAhmed",
      "extension": "101",
      "phone_number": "923001234567",
      "email": "ahmed@udhaar.pk",
      "status": "available",
      "is_active": true,
      "max_concurrent_calls": 1,
      "created_at": "2026-02-09T08:00:00Z",
      "updated_at": "2026-02-09T12:00:00Z"
    }
  ]
}
```

#### Create Agent
```
POST /api/dialer/agents/
```

**Request:**
```json
{
  "agent_id": "AgentAhmed",
  "extension": "101",
  "phone_number": "923001234567",
  "email": "ahmed@udhaar.pk",
  "is_active": true,
  "max_concurrent_calls": 1
}
```

**Response:** Returns created agent object

---

### 2. Campaign Management

#### List Campaigns
```
GET /api/dialer/campaigns/
```

Query Parameters:
- `agent` - Filter by agent ID
- `segment` - Filter by segment
- `status` - Filter by status
- `search` - Search by campaign_id or name
- `ordering` - Sort by created_at or updated_at

**Response:**
```json
{
  "count": 10,
  "results": [
    {
      "id": 1,
      "campaign_id": "AgentAhmed-Follow_Up-20260209",
      "campaign_name": "Ahmed Follow Up",
      "segment": "follow_up",
      "agent": 1,
      "agent_name": "AgentAhmed",
      "status": "active",
      "duration_days": 1,
      "created_at": "2026-02-09T08:30:00Z",
      "started_at": "2026-02-09T09:00:00Z",
      "completed_at": null,
      "updated_at": "2026-02-09T12:00:00Z",
      "total_leads": 14,
      "called_leads": 5,
      "completed_calls": 3,
      "failed_calls": 2,
      "no_answer_calls": 0,
      "leads_count": 14,
      "completion_percentage": 35.71,
      "metadata": {}
    }
  ]
}
```

#### Create Campaign
```
POST /api/dialer/campaigns/
```

**Request:**
```json
{
  "campaign_name": "Ahmed Follow Up",
  "segment": "follow_up",
  "agent": 1,
  "duration_days": 1,
  "metadata": {}
}
```

**Response:** Returns created campaign object
- `campaign_id` is auto-generated: `{agent_id}-{Segment}-{YYYYMMDD}`

#### Activate Campaign
```
POST /api/dialer/campaigns/{id}/activate/
```

**Response:**
```json
{
  "message": "Campaign activated",
  "campaign": { ... }
}
```

#### Get Campaign Statistics
```
GET /api/dialer/campaigns/{id}/stats/
```

**Response:**
```json
{
  "campaign_id": "AgentAhmed-Follow_Up-20260209",
  "total_leads": 14,
  "pending_leads": 9,
  "called_leads": 5,
  "completed_calls": 3,
  "failed_calls": 2,
  "no_answer_calls": 0,
  "total_calls": 5,
  "completion_rate": 35.71,
  "success_rate": 60.0
}
```

---

### 3. Lead Management

#### List Leads
```
GET /api/dialer/leads/
```

Query Parameters:
- `campaign` - Filter by campaign ID
- `segment` - Filter by segment
- `status` - Filter by status
- `search` - Search by phone_number or customer_name
- `ordering` - Sort by created_at or segment

**Response:**
```json
{
  "count": 14,
  "results": [
    {
      "id": 1,
      "lead_id": "AgentAhmed-Follow_Up-20260209-923001234567",
      "phone_number": "923001234567",
      "customer_name": "Ahmed Khan",
      "city": "Karachi",
      "segment": "follow_up",
      "campaign": 1,
      "campaign_name": "Ahmed Follow Up",
      "customer_segment": "small",
      "last_order_date": "2026-01-15T14:30:00Z",
      "last_call_date": null,
      "follow_up_date": "2026-02-09T00:00:00Z",
      "status": "pending",
      "attempt_count": 0,
      "max_attempts": 1,
      "metadata": {
        "order_count": 3,
        "feedback_status": "positive"
      },
      "created_at": "2026-02-09T08:31:20Z",
      "updated_at": "2026-02-09T08:31:20Z"
    }
  ]
}
```

#### Mark Lead as Called
```
POST /api/dialer/leads/{id}/mark_as_called/
```

**Response:**
```json
{
  "message": "Lead marked as called",
  "lead": { ... }
}
```

---

### 4. CSV Lead Upload (Replaces Telecard's assign_numbers_to_campaign)

#### Upload CSV File
```
POST /api/dialer/csv-upload/upload/
Content-Type: multipart/form-data
```

**Request:**
```
Form Data:
- campaign_id: "AgentAhmed-Follow_Up-20260209" (string)
- segment: "follow_up" (string)
- csv_file: <binary file> (file)
```

**CSV File Format:**
```csv
name,number,city,attempts
Ahmed Khan,923001234567,Karachi,1
Fatima Ali,923009876543,Lahore,1
Hassan Malik,923005555555,Islamabad,1
```

**Response:**
```json
{
  "import_id": "550e8400-e29b-41d4-a716-446655440000",
  "campaign_id": "AgentAhmed-Follow_Up-20260209",
  "filename": "AgentAhmed-Follow_Up.csv",
  "status": "completed",
  "total_rows": 3,
  "successful_imports": 3,
  "failed_imports": 0,
  "skipped_rows": 0,
  "success_rate": 100.0,
  "errors": [],
  "summary": "Processed 3 rows. Successful: 3, Failed: 0, Skipped: 0"
}
```

**Phone Number Normalization:**
- Removes "ap" prefix (country code variant)
- Removes "b" prefix (alternate format)
- Validates 11 digits, all numeric
- Must start with "92" (Pakistan country code)

**Error Handling:**
```json
{
  "import_id": "...",
  "status": "partial",
  "errors": [
    {
      "row": 5,
      "number": "invalid123",
      "error": "Invalid phone number format"
    },
    {
      "row": 8,
      "number": "923001234567",
      "error": "Duplicate phone number in campaign"
    }
  ]
}
```

---

### 5. Import Logs

#### List Import Logs
```
GET /api/dialer/import-logs/
```

Query Parameters:
- `campaign` - Filter by campaign
- `status` - Filter by status
- `ordering` - Sort by created_at

---

### 6. Call Logs

#### List Call Logs
```
GET /api/dialer/call-logs/
```

Query Parameters:
- `agent` - Filter by agent
- `campaign` - Filter by campaign
- `lead` - Filter by lead
- `status` - Filter by call status
- `search` - Search by call_id, phone_number, freeswitch_uuid

---

## Migration Guide: From Telecard to Voice Orchestrator

### Step 1: Prepare Environment

```bash
# Install dependencies
pip install django djangorestframework django-filter

# Run migrations
python manage.py makemigrations
python manage.py migrate
```

### Step 2: Create Agents

```bash
curl -X POST http://localhost:8000/api/dialer/agents/ \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "AgentAhmed",
    "extension": "101",
    "phone_number": "923001234567",
    "email": "ahmed@udhaar.pk",
    "is_active": true,
    "max_concurrent_calls": 1
  }'
```

### Step 3: Create Campaign

```bash
curl -X POST http://localhost:8000/api/dialer/campaigns/ \
  -H "Content-Type: application/json" \
  -d '{
    "campaign_name": "Ahmed Follow Up",
    "segment": "follow_up",
    "agent": 1,
    "duration_days": 1
  }'
```

### Step 4: Upload CSV with Leads

```bash
curl -X POST http://localhost:8000/api/dialer/csv-upload/upload/ \
  -F "campaign_id=AgentAhmed-Follow_Up-20260209" \
  -F "segment=follow_up" \
  -F "csv_file=@leads.csv"
```

### Step 5: Activate Campaign

```bash
curl -X POST http://localhost:8000/api/dialer/campaigns/1/activate/ \
  -H "Content-Type: application/json"
```

---

## Database Queries

### Get pending leads for a campaign

```python
from dialer.models import Lead

leads = Lead.objects.filter(
    campaign_id=1,
    status='pending'
).order_by('segment', '-created_at')
```

### Get campaign statistics

```python
from dialer.models import Campaign

campaign = Campaign.objects.get(id=1)
stats = {
    'total_leads': campaign.total_leads,
    'completion_rate': (campaign.called_leads / campaign.total_leads * 100) 
                       if campaign.total_leads > 0 else 0,
    'success_rate': (campaign.completed_calls / campaign.called_leads * 100) 
                    if campaign.called_leads > 0 else 0,
}
```

### Get all calls for an agent

```python
from dialer.models import CallLog

calls = CallLog.objects.filter(agent_id=1).order_by('-initiated_at')
```

---

## Error Handling

### Common Error Responses

**Invalid Campaign:**
```json
{
  "error": "Campaign AgentAhmed-Follow_Up-20260209 not found"
}
```

**CSV Validation Error:**
```json
{
  "csv_file": ["CSV missing required columns: {'number', 'attempts'}"]
}
```

**Invalid Phone Number:**
```json
{
  "error": "Error processing CSV: Invalid phone number format at row 5"
}
```

---

## Performance Considerations

### Indexes
All models include strategic indexes for common queries:
- Campaign: `(campaign_id)`, `(agent, status)`, `(segment, status)`
- Lead: `(phone_number)`, `(campaign, status)`, `(segment, status)`
- LeadImportLog: `(import_id)`, `(campaign)`, `(status)`
- CallLog: Enhanced with lead and campaign references

### Batch Operations
- CSV uploads are processed in database transactions
- Atomic import operations ensure data consistency
- Error logging allows partial successes

### Caching
- Campaign statistics can be cached (Redis)
- Agent status is cached with 10-minute TTL
- Lead counts per campaign updated after each import

---

## Development Notes

### Adding Custom Fields
To add fields to Lead or Campaign:
1. Update the model in `dialer/models.py`
2. Run `makemigrations` and `migrate`
3. Update serializers if needed
4. Create API tests

### Extending Import Logic
To customize CSV import behavior:
1. Override `_process_csv()` in `CSVLeadUploadViewSet`
2. Add custom validation in `_normalize_phone()`
3. Implement additional error handling

---

## Support & Troubleshooting

### Common Issues

**"Campaign not found"**
- Ensure campaign_id format matches: `{AgentId}-{Segment}-{YYYYMMDD}`
- Check campaign status is 'draft' or 'active' before uploading

**"Duplicate phone number"**
- Ensure CSV doesn't contain duplicate phone numbers
- Check if leads were previously imported

**"Invalid phone format"**
- Phone must be 11 digits starting with 92
- Prefixes "ap" and "b" are automatically removed
- Use format: 923001234567

---

## Appendix: Complete Migration Checklist

- [ ] Install Django REST Framework and django-filter
- [ ] Run database migrations
- [ ] Create agents in new system
- [ ] Export campaigns from Telecard
- [ ] Create campaigns in Voice Orchestrator
- [ ] Export lead CSVs from Udhaar
- [ ] Upload CSVs via API
- [ ] Verify lead counts and data integrity
- [ ] Activate campaigns
- [ ] Monitor calls and update campaign statistics
- [ ] Deactivate Telecard campaigns
