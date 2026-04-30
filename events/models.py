from django.db import models
from dialer.models import Agent


class AgentLogs(models.Model):
    ACTION_ENUM = (
        ('login', 'login'),
        ('logout', 'logout'),
        ('campaign_selection', 'campaign_selection'),
        ('form_submission', 'form_submission'),
        ('call_reject', 'call_reject')
    )
    
    agent = models.ForeignKey(
        Agent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='logs',
        help_text="Agent associated with this log entry"
    )

    action = models.CharField(
        max_length=50,
        choices=ACTION_ENUM,
        db_index=True
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional context for this action"
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['agent', 'action']),
            models.Index(fields=['created_at']),
        ]
        verbose_name = 'Agent Log'
        verbose_name_plural = 'Agent Logs'

    def __str__(self):
        agent_label = self.agent_id if self.agent_id else 'Unknown agent'
        return f"{agent_label} - {self.action} at {self.created_at}"
