"""
Dialer Models - Core VoIP dialer orchestration entities.

Models for managing agents, campaigns, calls, and dialer configurations.
"""

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
import uuid


class Agent(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Associated Django user (optional)"
    )
    extension = models.CharField(
        max_length=20,
        unique=True,
        help_text="SIP/VoIP extension (e.g., 101, 201)"
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text="Whether agent is enabled in the system"
    )

    freeswitch_password = models.CharField(
        max_length=255,
        help_text="FreeSWITCH password for the agent extension",
        null=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.id} ({self.extension})"


class CallLog(models.Model):
    STATUS_CHOICES = [
        ('answered', 'Answered'),
        ('failed', 'Failed'),
        ('no_answer', 'No Answer'),
        ('busy', 'Busy'),
        ('invalid', 'Invalid Number'),
        ('cancelled', 'Cancelled'),
    ]
    
    call_id = models.CharField(
        max_length=100,
        unique=True,
        default=uuid.uuid4,
        db_index=True,
        help_text="Unique call identifier (UUID)"
    )
    
    # Parties involved
    agent = models.ForeignKey(
        Agent,
        on_delete=models.SET_NULL,
        null=True,
        related_name='calls',
        help_text="Agent handling the call"
    )
    lead = models.ForeignKey(
        'Lead',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='calls',
        help_text="Lead being called"
    )
    campaign = models.ForeignKey(
        'Campaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='calls',
        help_text="Campaign this call belongs to"
    )
    
    # Call endpoints
    from_extension = models.CharField(
        max_length=20,
        help_text="Agent extension (originating)",
        null=True
    )
    to_number = models.CharField(
        max_length=20,
        help_text="Destination phone number"
    )
    
    # Status & Result
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='initiated',
        db_index=True
    )
    disconnect_reason = models.CharField(
        max_length=100,
        blank=True,
        help_text="Reason for call termination (e.g., NORMAL_CLEARING, NO_ANSWER)"
    )
    
    # Timing
    initiated_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Call duration in seconds"
    )
    talk_time_seconds = models.PositiveIntegerField(
        default=0,
        help_text="Actual talk time in seconds"
    )
    
    # Retry tracking
    attempt_number = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Retry attempt number",
        null=True
    )

    # retry_after = models.DateTimeField(
    #     null=True,
    #     blank=True,
    #     help_text="Schedule for next retry"
    # )

    call_direction = models.CharField(max_length=20)
    
    # Recording & Quality
    recording_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="URL to call recording"
    )
    
    class Meta:
        ordering = ['-initiated_at']
        # indexes = [
        #     models.Index(fields=['call_id']),
        #     models.Index(fields=['freeswitch_uuid']),
        #     models.Index(fields=['agent', 'initiated_at']),
        #     models.Index(fields=['campaign', 'status']),
        #     models.Index(fields=['status', 'initiated_at']),
        #     models.Index(fields=['to_number']),
        # ]
    
    def __str__(self):
        return f"Call {self.call_id}: {self.from_extension} → {self.to_number} ({self.status})"


class Campaign(models.Model):
    """
    Campaign model representing a calling campaign (replacement for Telecard).
    
    Stores campaign metadata, agent assignments, and status tracking.
    Replaces Telecard's campaign creation and management functionality.
    """
    
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('archived', 'Archived'),
    ]
    
    campaign_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique campaign identifier (e.g., AgentAhmed-Follow_Up-20260209)"
    )
    
    campaign_name = models.CharField(
        max_length=255,
        help_text="Human-readable campaign name"
    )

    active = models.BooleanField(
        default=True
    )
    
    # Segment classification
    segment = models.CharField(
        max_length=50,
        choices=[
            ('follow_up', 'Follow Up'),
            ('active', 'Active'),
            ('growth', 'Growth'),
            ('active_churn', 'Active Churn'),
            ('growth_churn', 'Growth Churn'),
            ('acquisition', 'Acquisition'),
        ],
        db_index=True,
        help_text="Lead segment classification"
    )
    
    # Agent assignment
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        null=True,
        related_name='campaigns',
        help_text="Assigned agent (telesales representative)"
    )
    
    # Status & scheduling
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='draft',
        db_index=True
    )
    
    duration_days = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
        help_text="Campaign duration in days"
    )
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Statistics
    # called_leads = models.PositiveIntegerField(
    #     default=0,
    #     help_text="Leads that have been dialed"
    # )
    # completed_calls = models.PositiveIntegerField(
    #     default=0,
    #     help_text="Successfully completed calls"
    # )
    # failed_calls = models.PositiveIntegerField(
    #     default=0,
    #     help_text="Failed call attempts"
    # )
    # no_answer_calls = models.PositiveIntegerField(
    #     default=0,
    #     help_text="Calls with no answer"
    # )
    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional campaign context (campaign type, filters, etc.)"
    )
    
    class Meta:
        ordering = ['-created_at']
        # indexes = [
        #     models.Index(fields=['campaign_id']),
        #     models.Index(fields=['agent', 'status']),
        #     models.Index(fields=['segment', 'status']),
        #     models.Index(fields=['status', 'created_at']),
        # ]
        unique_together = [['agent', 'segment', 'created_at']]
    
    def __str__(self):
        return f"{self.campaign_id} ({self.segment} - {self.status})"


class Lead(models.Model):
    """
    Lead model representing a customer to be called (from Udhaar marketplace).
    
    Stores lead/customer information imported from CSV uploads.
    Replaces Telecard's call queue items.
    """
    
    # SEGMENT_CHOICES = [
    #     ('follow_up', 'Follow Up'),
    #     ('active', 'Active'),
    #     ('growth', 'Growth'),
    #     ('active_churn', 'Active Churn'),
    #     ('growth_churn', 'Growth Churn'),
    #     ('acquisition', 'Acquisition'),
    # ] since it's in campaigns
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_queue', 'In Queue'),
        ('not_answered', 'Not Answered'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('invalid', 'Invalid'),
    ]
    
    # Unique identifiers
    id = models.IntegerField(
        primary_key=True,
        db_index=True,
    )

    udhaar_lead_id = models.CharField(
        max_length=100,
        # unique=True,
        db_index=True,
        help_text="Unique lead identifier (from Udhaar)"
    )

    phone_number = models.CharField(
        max_length=20,
        db_index=True,
        help_text="Customer phone number (normalized, e.g., 923001234567)"
    )
    
    # Customer information
    customer_name = models.CharField(
        max_length=255,
        help_text="Customer/lead full name"
    )
    city = models.CharField(
        max_length=100,
        blank=True,
        help_text="Customer city/location"
    )
    
    # Campaign association
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name='leads',
        help_text="Associated campaign",
        null=True
    )
    
    # Lead metadata from Udhaar
    customer_segment = models.CharField(
        max_length=50,
        blank=True,
        help_text="Business segment (micro/small/medium/large)"
    )
    
    last_call_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last contact/call date"
    )

    last_order_details = models.JSONField(
        default=dict,
        null=True,
        blank=True
    )

    month_gmv = models.FloatField(
        null=True,
        blank=True,
        help_text="Last contact/call date"
    )

    overall_gmv = models.FloatField(
        null=True,
        blank=True,
        help_text="Last contact/call date"
    )
    
    # Call tracking
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    attempt_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of call attempts"
    )
    max_attempts = models.PositiveIntegerField(
        default=1,
        help_text="Maximum allowed call attempts"
    )
    call_completed = models.BooleanField(
        default=False
    )
    lead_contacted = models.BooleanField(
        default=False
    )


    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional lead context (Active shop, personal use, category, etc.)"
    )

    # active_shop = models.BooleanField(
    #     default=False,
    #     blank=True
    # )

    # active_shop = models.BooleanField(
    #     default=False,
    #     blank=True
    # )
    # active_shop = models.BooleanField(
    #     default=False,
    #     blank=True
    # )
    # active_shop = models.BooleanField(
    #     default=False,
    #     blank=True
    # )
    # active_shop = models.BooleanField(
    #     default=False,
    #     blank=True
    # )
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        # indexes = [
        #     models.Index(fields=['lead_id']),
        #     models.Index(fields=['phone_number']),
        #     models.Index(fields=['campaign', 'status']),
        #     models.Index(fields=['segment', 'status']),
        #     models.Index(fields=['status']),
        # ]
        # unique_together = [['campaign', 'phone_number']]
    
    def __str__(self):
        return f"{self.customer_name} ({self.phone_number})"