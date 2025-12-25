from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .models import Notification, NotificationTemplate, UserNotificationPreference, UserDevice
from .serializers import NotificationSerializer, NotificationTemplateSerializer, UserNotificationPreferenceSerializer, UserDeviceSerializer

class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_staff

class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(recipient_user=self.request.user)

    @action(detail=False, methods=['post'])
    def ack(self, request):
        ids = request.data.get('ids', [])
        qs = Notification.objects.filter(id__in=ids, recipient_user=request.user)
        qs.update(read=True)
        return Response({'ok': True})

class TemplateViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminOrReadOnly]
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.all()
    lookup_field = 'key'

class PreferenceViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserNotificationPreferenceSerializer

    def get_queryset(self):
        return UserNotificationPreference.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class UserDeviceViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserDeviceSerializer

    def get_queryset(self):
        return UserDevice.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
