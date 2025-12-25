from django.db import models
from django.conf import settings
from django.template import Template, Context
from django.utils import timezone
import uuid

class NotificationTopic(models.TextChoices):
    FEES = 'fees', 'Frais'
    GRADES = 'grades', 'Notes'
    BULLETIN = 'bulletin', 'Bulletin'
    ATTENDANCE = 'attendance', 'Absences'
    MESSAGE = 'message', 'Messagerie'
    TIMETABLE = 'timetable', 'Emploi du temps'

class Channel(models.TextChoices):
    INAPP = 'inapp', 'In-App'
    EMAIL = 'email', 'Email'
    SMS = 'sms', 'SMS'
    PUSH = 'push', 'Push'

class NotificationTemplate(models.Model):
    key = models.CharField(max_length=120, unique=True)
    topic = models.CharField(max_length=50, choices=NotificationTopic.choices)
    title_template = models.CharField(max_length=200)
    body_template = models.TextField()
    default_channels = models.JSONField(default=list)

    def __str__(self):
        return f"{self.key} ({self.topic})"

class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(NotificationTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    topic = models.CharField(max_length=50, choices=NotificationTopic.choices)
    recipient_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    payload = models.JSONField(default=dict)
    channels = models.JSONField(default=list)
    sent = models.BooleanField(default=False)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['recipient_user', 'created_at']),
            models.Index(fields=['topic', 'created_at']),
        ]
        ordering = ['-created_at']

    def clean(self):
        if self.template and self.template.topic != self.topic:
            from django.core.exceptions import ValidationError
            raise ValidationError("Le template doit appartenir au même topic que la notification.")

    def render_title(self):
        try:
            if self.template and self.template.title_template:
                # Context() accepte un dict. Si payload est None, on met {}
                ctx = Context(self.payload or {})
                return Template(self.template.title_template).render(ctx)
        except Exception as e:
            # Fallback de sécurité : on renvoie le titre brut ou une erreur masquée
            return self.payload.get('title', f"Notification {self.topic}")
        return self.payload.get('title') or ''
    def render_body(self):
        if self.template and self.template.body_template:
            return Template(self.template.body_template).render(Context(self.payload))
        return self.payload.get('body') or ''

    def mark_sent(self, sent_at=None):
        self.sent = True
        self.sent_at = sent_at or timezone.now()
        self.save(update_fields=['sent', 'sent_at'])

    def mark_read(self):
        self.read = True
        self.save(update_fields=['read'])

class UserNotificationPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    topic = models.CharField(max_length=50, choices=NotificationTopic.choices)
    channels = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'topic')

class UserDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='devices')
    provider = models.CharField(max_length=30, default='fcm')
    token = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)

class NotificationAttempt(models.Model):
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name='attempts')
    channel = models.CharField(max_length=20, choices=Channel.choices)
    tried_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)
    response = models.TextField(null=True, blank=True)
