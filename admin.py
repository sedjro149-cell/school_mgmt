from django.contrib import admin
from .models import Notification, NotificationTemplate, UserNotificationPreference, UserDevice, NotificationAttempt

@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ('key','topic')
    search_fields = ('key','topic')

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id','topic','recipient_user','sent','read','created_at')
    list_filter = ('topic','sent','read')
    search_fields = ('recipient_user__username','payload')

@admin.register(UserNotificationPreference)
class UserNotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user','topic','enabled')

@admin.register(UserDevice)
class UserDeviceAdmin(admin.ModelAdmin):
    list_display = ('user','provider','token','created_at')

@admin.register(NotificationAttempt)
class NotificationAttemptAdmin(admin.ModelAdmin):
    list_display = ('notification','channel','tried_at','success')
    list_filter = ('channel','success')
