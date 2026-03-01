from rest_framework import viewsets, permissions, status, filters as drf_filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.db.models import Q

from .models import Notification, NotificationTemplate, UserNotificationPreference, UserDevice
from .serializers import (
    NotificationSerializer,
    NotificationTemplateSerializer,
    UserNotificationPreferenceSerializer,
    UserDeviceSerializer,
)
from .delivery import send_notification


class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_staff


class TemplateViewSet(viewsets.ModelViewSet):
    """
    Admin: CRUD des templates
    """
    permission_classes = [IsAdminOrReadOnly]
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.all()
    lookup_field = 'key'
    filter_backends = [drf_filters.SearchFilter, drf_filters.OrderingFilter]
    search_fields = ['key', 'topic']
    ordering_fields = ['key', 'topic']


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Lecture des notifications pour les utilisateurs.
    Admins peuvent lister toutes les notifications et filtrer par user (?user=ID).
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSerializer
    queryset = Notification.objects.select_related('recipient_user', 'template').all()
    filter_backends = [drf_filters.SearchFilter, drf_filters.OrderingFilter]
    search_fields = ['topic', 'recipient_user__username']
    ordering_fields = ['created_at', 'sent_at', 'read']
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        # staff can filter by user id
        if user.is_staff or user.is_superuser:
            user_id = self.request.query_params.get('user', None)
            if user_id:
                qs = qs.filter(recipient_user__id=user_id)
            return qs
        # normal user only sees their own notifications
        return qs.filter(recipient_user=user)

    @action(detail=False, methods=['post'])
    def ack(self, request):
        """
        Marquer en lot des notifications comme lues.
        Body: {"ids": ["uuid1","uuid2", ...]}
        """
        ids = request.data.get('ids', []) or []
        if not isinstance(ids, (list, tuple)):
            return Response({"detail": "ids should be a list"}, status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        qs = Notification.objects.filter(id__in=ids)
        if not (user.is_staff or user.is_superuser):
            qs = qs.filter(recipient_user=user)
        updated = qs.update(read=True)
        return Response({"ok": True, "updated": updated})

    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        """
        Marquer une notif comme lue (user doit être destinataire).
        """
        user = request.user
        notif = get_object_or_404(Notification, pk=pk)
        if not (user.is_staff or user.is_superuser) and notif.recipient_user != user:
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        notif.read = True
        notif.save(update_fields=['read'])
        return Response({"detail": "Notification marquée comme lue."})

    @action(detail=True, methods=['post'])
    def resend(self, request, pk=None):
        """
        Permet à un admin de renvoyer une notification (par ex pour debug).
        """
        user = request.user
        if not (user.is_staff or user.is_superuser):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        notif = get_object_or_404(Notification, pk=pk)
        try:
            send_notification(notif)
        except Exception as e:
            return Response({"detail": "erreur en renvoyant", "error": str(e)}, status=500)
        return Response({"detail": "Notification renvoyée."})


class PreferenceViewSet(viewsets.ModelViewSet):
    """
    Chaque user gère ses préférences. Admin peut voir toutes.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserNotificationPreferenceSerializer
    queryset = UserNotificationPreference.objects.select_related('user').all()
    filter_backends = [drf_filters.SearchFilter, drf_filters.OrderingFilter]
    search_fields = ['topic', 'user__username']
    ordering_fields = ['topic']

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_staff or user.is_superuser:
            user_id = self.request.query_params.get('user', None)
            if user_id:
                qs = qs.filter(user__id=user_id)
            return qs
        return qs.filter(user=user)

    def perform_create(self, serializer):
        # toujours associer l'utilisateur courant
        if self.request.user.is_staff or self.request.user.is_superuser:
            # admin peut créer pour d'autres si user envoyé
            serializer.save()
        else:
            serializer.save(user=self.request.user)


class UserDeviceViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = UserDeviceSerializer
    queryset = UserDevice.objects.select_related('user').all()

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            user_id = self.request.query_params.get('user', None)
            if user_id:
                return self.queryset.filter(user__id=user_id)
            return self.queryset
        return self.queryset.filter(user=user)

    def perform_create(self, serializer):
        # associe automatiquement l'utilisateur courant sauf admin
        if self.request.user.is_staff or self.request.user.is_superuser:
            serializer.save()
        else:
            serializer.save(user=self.request.user)
