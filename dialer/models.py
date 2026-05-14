"""
Dialer Models - Core VoIP dialer orchestration entities.

Models for managing agents, campaigns, calls, and dialer configurations.
"""

from django.conf import settings
from django.db import models, transaction
from django.contrib.auth.models import User
from voice_orchestrator.utils import fs_create_user, fs_delete_user
from django.core.exceptions import ValidationError
import uuid


class Team(models.Model):
    NAME_CHOICES = [
        ('saddar_growth', "Saddar Growth"),
        ('rupin_emi', "Rupin EMI"),
    ]

    name = models.CharField(max_length=100, unique=True, choices=NAME_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


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

    telecard_username = models.CharField(
        max_length=50, null=True, blank=True
    )

    udhaar_username = models.CharField(
        max_length=50, null=True, blank=True
    )

    teams = models.ManyToManyField(
        Team,
        blank=True,
        related_name='agents',
    )

    selected_campaign = models.ForeignKey(
        'Campaign',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='selected_agents',
        help_text="Currently selected campaign for this agent"
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def _get_freeswitch_group(self) -> str | None:
        if self.user is None:
            return None

        first_group = self.user.groups.first()
        if first_group is not None:
            return first_group.name

        return getattr(settings, 'DEFAULT_FS_GROUP', None)

    def _sync_freeswitch_user(self, old_agent = None) -> None:
        if not self.extension or not self.freeswitch_password:
            if old_agent is not None and old_agent.extension:
                fs_delete_user(old_agent.extension)
            return

        current_group = self._get_freeswitch_group()

        if old_agent is None:
            fs_create_user(self.extension, self.freeswitch_password, current_group)
            return

        if old_agent.extension != self.extension:
            if old_agent.extension:
                fs_delete_user(old_agent.extension)
            fs_create_user(self.extension, self.freeswitch_password, current_group)
            return

        old_group = old_agent._get_freeswitch_group()
        if old_group != current_group:
            fs_delete_user(self.extension)
            fs_create_user(self.extension, self.freeswitch_password, current_group)
            return

        fs_create_user(self.extension, self.freeswitch_password, current_group)

    def save(self, *args, **kwargs):
        if self.user is not None and not self.user.groups.exists():
            raise ValidationError("The associated user must belong to at least one group.")

        # if self.pk is not None and not self.teams.exists():
        #     raise ValidationError("Agent must belong to at least one team.")

        old_agent = None
        if self.pk is not None:
            old_agent = Agent.objects.filter(pk=self.pk).first()

        with transaction.atomic():
            super().save(*args, **kwargs)
            self._sync_freeswitch_user(old_agent) 

    def delete(self, *args, **kwargs):
        extension = self.extension
        with transaction.atomic():
            super().delete(*args, **kwargs)
            if extension:
                fs_delete_user(extension)

    def __str__(self):
        return f"{self.id} ({self.extension})"


class CallLog(models.Model):
    STATUS_CHOICES = [
        ('answered', 'Answered'),
        ('failed', 'Failed'),
        ('no_answer', 'No Answer'),
        ('busy', 'Busy'),
        ('lose_race', 'Lose Race'),
        ('user_not_registered', 'User Not Registered'),
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

    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Call {self.call_id}: → ({self.status})"


class CallLogExports(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    filepath = models.CharField(max_length=500, blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    filters = models.JSONField(default=dict, blank=True)
    total_recordings = models.PositiveIntegerField(default=0)
    exported_recordings = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Call Log Export'
        verbose_name_plural = 'Call Log Exports'

    def __str__(self):
        return f"Call log export {self.id} ({self.status})"


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

    CAMPAIGN_TYPE_CHOICES = [
        ('saddar_growth', 'Saddar Growth'), #every day campaign for saddar
        ('rupin_emi', 'Rupin EMI') #rupin emi followups. 
    ]

    SEGMENT_CHOICES = [
        ('follow_up', 'Follow Up'),
        ('active', 'Active'),
        ('growth', 'Growth'),
        ('active_churn', 'Active Churn'),
        ('growth_churn', 'Growth Churn'),
        ('acquisition', 'Acquisition'),
        ('other', 'Other'), 
    ]
    
    campaign_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Unique campaign identifier (e.g., AgentAhmed-Follow_Up-20260209)"
    )
    
    campaign_name = models.CharField(
        max_length=255,
        help_text="Human-readable campaign name",
        null=True
    )

    active = models.BooleanField(
        default=True
    )

    campaign_type = models.CharField(
        max_length=30,
        choices=CAMPAIGN_TYPE_CHOICES,
        null=True,
        blank=True
    )
    
    # Segment classification
    segment = models.CharField(
        max_length=50,
        choices=SEGMENT_CHOICES,
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
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional campaign context (campaign type, filters, etc.)"
    ) #not used atm
    
    class Meta:
        ordering = ['-created_at']
        unique_together = [['agent', 'segment', 'created_at']]
    
    def __str__(self):
        return f"{self.campaign_id} ({self.segment})"


class Lead(models.Model):   
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_queue', 'In Queue'),
        ('not_answered', 'Not Answered'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('invalid', 'Invalid'),
    ] 

    udhaar_lead_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique lead identifier (from Udhaar)"
    )

    dukaan_account_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="Dukaan account identifier"
    )
    
    emi_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique EMI lead identifier (from Udhaar)"
    )

    emi_converted = models.BooleanField(default=False, null=True)

    emi_stage = models.CharField(
        max_length=50,
        blank=True,
        null=True
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
        null=True,
        help_text="Customer city/location"
    )

    address = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Customer address"
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

    total_orders = models.IntegerField(
        null=True,
        blank=True,
        help_text="Total number of orders"
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

    comment = models.CharField(max_length=255, null=True, blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    follow_up_time = models.TimeField(null=True, blank=True)
    
    # Metadata
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional lead context (Active shop, personal use, category, etc.)"
    )
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.customer_name} ({self.phone_number})"
