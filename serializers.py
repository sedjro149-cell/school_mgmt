from rest_framework import serializers
from .models import Notification, NotificationTemplate, UserNotificationPreference, UserDevice

class NotificationTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationTemplate
        fields = ('key','topic','title_template','body_template','default_channels')

class NotificationSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    body = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ('id','topic','title','body','channels','sent','read','created_at','payload')

    def get_title(self, obj):
        return obj.render_title()

    def get_body(self, obj):
        return obj.render_body()

class UserNotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserNotificationPreference
        fields = ('user','topic','channels','enabled')
        read_only_fields = ('user',)

class UserDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDevice
        fields = ('id','provider','token','created_at')
        read_only_fields = ('created_at',)
