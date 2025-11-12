# academics/permissions.py
from rest_framework import permissions

class IsAdminOrReadOnly(permissions.BasePermission):
    """
    - Admin / staff → tout CRUD
    - Parent / Student → lecture seule (GET)
    """

    def has_permission(self, request, view):
        if request.user.is_staff or request.user.is_superuser:
            return True
        return request.method in permissions.SAFE_METHODS

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True
        return request.method in permissions.SAFE_METHODS


class IsAdminOrParentReadOnly(permissions.BasePermission):
    """
    - Admins → tout
    - Parents → lecture seule sur leurs enfants (filtrage fait dans get_queryset)
    """

    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True
        return request.method in permissions.SAFE_METHODS
