from rest_framework import permissions

class IsStudentOrParentOrAdmin(permissions.BasePermission):
    """
    Admin: tout accès
    Parent: lecture sur les enfants
    Étudiant: lecture sur lui-même
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user

        if user.is_staff or user.is_superuser:
            return True

        if hasattr(user, "student") and obj.student == user.student:
            return request.method in permissions.SAFE_METHODS

        if hasattr(user, "parent") and obj.student.parent == user.parent:
            return request.method in permissions.SAFE_METHODS

        return False
