from collections import defaultdict
from rest_framework import serializers
from .models import Notification, NotificationTemplate, UserNotificationPreference, UserDevice
import logging

# On ajoute un logger pour voir les erreurs dans la console Django/Docker
logger = logging.getLogger(__name__)



class NotificationTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationTemplate
        fields = ('key', 'topic', 'title_template', 'body_template', 'default_channels')

# notifications/serializers.py (patch pour NotificationSerializer)
from .utils import render_django_template

class NotificationSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    body = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ('id', 'topic', 'title', 'body', 'channels', 'sent', 'read', 'created_at', 'payload')

    def _get_template(self, obj):
        tpl = getattr(obj, 'template', None)
        if tpl:
            return tpl
        if obj.topic:
            return NotificationTemplate.objects.filter(topic=obj.topic).first()
        return None

    def get_title(self, obj):
        # First: use model helper if defined
        try:
            if hasattr(obj, 'render_title') and callable(getattr(obj, 'render_title')):
                t = obj.render_title()
                if t:
                    return t
        except Exception:
            pass

        # Next: template via DB with Django rendering
        tpl = self._get_template(obj)
        if tpl and getattr(tpl, 'title_template', None):
            return render_django_template(tpl.title_template, obj.payload or {})

        # Fallback to payload keys
        payload = obj.payload or {}
        if isinstance(payload, dict):
            for k in ('title', 'subject', 'headline'):
                if payload.get(k):
                    return str(payload.get(k))
        topic = getattr(obj, 'topic', None) or ''
        return f"Notification - {topic.capitalize()}"

    def get_body(self, obj):
        try:
            if hasattr(obj, 'render_body') and callable(getattr(obj, 'render_body')):
                b = obj.render_body()
                if b:
                    return b
        except Exception:
            pass

        tpl = self._get_template(obj)
        if tpl and getattr(tpl, 'body_template', None):
            return render_django_template(tpl.body_template, obj.payload or {})

        payload = obj.payload or {}
        if isinstance(payload, dict):
            for k in ('body', 'message', 'text'):
                if payload.get(k):
                    return str(payload.get(k))

            parts = []
            if payload.get('parent_name'):
                parts.append(f"Parent: {payload.get('parent_name')}")
            if payload.get('student_name'):
                parts.append(f"Élève: {payload.get('student_name')}")
            amt = payload.get('amount') if payload.get('amount') is not None else payload.get('amount_due')
            if payload.get('fee_type'):
                parts.append(f"Type: {payload.get('fee_type')}")
            if amt is not None:
                parts.append(f"Montant: {amt}")
            if payload.get('due_date'):
                parts.append(f"Echéance: {payload.get('due_date')}")
            ref = payload.get('invoice_ref') or payload.get('reference')
            if ref:
                parts.append(f"Réf: {ref}")
            if parts:
                return " — ".join(parts)

        return "(Aucun contenu)"


class UserNotificationPreferenceSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = UserNotificationPreference
        fields = ('id', 'user', 'username', 'topic', 'channels', 'enabled')
        read_only_fields = ('id', 'username')

    def validate(self, attrs):
        topic = attrs.get('topic') or (self.instance.topic if self.instance else None)
        field = UserNotificationPreference._meta.get_field('topic')
        valid = [c[0] for c in getattr(field, 'choices', [])]
        if topic not in valid:
            raise serializers.ValidationError({"topic": "Topic invalide."})
        return super().validate(attrs)


class UserDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDevice
        fields = ('id', 'user', 'provider', 'token', 'created_at')
        read_only_fields = ('id', 'created_at')

    def validate(self, attrs):
        user = attrs.get('user') or (self.instance.user if self.instance else None)
        token = attrs.get('token') or (self.instance.token if self.instance else None)
        provider = attrs.get('provider') or (self.instance.provider if self.instance else None)
        if user and token and provider:
            qs = UserDevice.objects.filter(user=user, provider=provider, token=token)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError("Ce device token existe déjà pour cet utilisateur.")
        return super().validate(attrs)