from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NotificationViewSet, TemplateViewSet, PreferenceViewSet, UserDeviceViewSet

router = DefaultRouter()
router.register(r'', NotificationViewSet, basename='notifications')
router.register(r'templates', TemplateViewSet, basename='notification-templates')
router.register(r'preferences', PreferenceViewSet, basename='notification-preferences')
router.register(r'devices', UserDeviceViewSet, basename='notification-devices')

urlpatterns = [
    path('', include(router.urls)),  # <-- juste '', pas 'api/notifications/'
]
