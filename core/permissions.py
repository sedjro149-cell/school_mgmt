# core/permissions.py
from rest_framework import permissions


class IsParentOrReadOnly(permissions.BasePermission):
    """
    - Un parent ne peut voir QUE ses propres infos et celles de ses enfants.
    - Un parent n’a pas le droit de modifier/supprimer.
    - Les admins (is_staff ou superuser) peuvent tout faire.
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True

        if hasattr(request.user, "parent"):
            parent = request.user.parent

            # Parent → peut voir son propre profil
            if obj.__class__.__name__ == "Parent" and obj == parent:
                return request.method in permissions.SAFE_METHODS

            # Parent → peut voir uniquement ses enfants
            if obj.__class__.__name__ == "Student" and obj.parent == parent:
                return request.method in permissions.SAFE_METHODS

        return False


class IsStudentOrParent(permissions.BasePermission):
    """
    - Parent : ne voit que ses enfants
    - Étudiant : ne voit que son propre profil
    - Admin : voit tout
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True

        if hasattr(request.user, "parent") and obj.__class__.__name__ == "Student":
            return obj.parent == request.user.parent

        if hasattr(request.user, "student") and obj.__class__.__name__ == "Student":
            return obj == request.user.student

        return False


class IsTeacherReadOnly(permissions.BasePermission):
    """
    - Un enseignant peut voir uniquement :
        - Ses propres infos
        - Les élèves liés aux classes qu'il enseigne
        - Les notes des élèves de ses classes
    - Pas de modification autorisée
    - Les admins voient tout
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True

        if hasattr(request.user, "teacher") and request.method in permissions.SAFE_METHODS:
            teacher = request.user.teacher

            if obj.__class__.__name__ == "Student":
                return obj.school_class in teacher.classes.all()

            if obj.__class__.__name__ == "Grade":
                return obj.student.school_class in teacher.classes.all()

            if obj.__class__.__name__ == "Teacher":
                return obj == teacher

        return False


class IsParentOrTeacherOrReadOnly(permissions.BasePermission):
    """
    - Parent : accès lecture seule à ses enfants
    - Teacher : accès lecture seule aux élèves de ses classes
    - Admin : accès total
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True

        # Parent
        if hasattr(request.user, "parent") and obj.__class__.__name__ == "Student":
            return obj.parent == request.user.parent and request.method in permissions.SAFE_METHODS

        # Teacher
        if hasattr(request.user, "teacher") and request.method in permissions.SAFE_METHODS:
            teacher = request.user.teacher
            if obj.__class__.__name__ == "Student":
                return obj.school_class in teacher.classes.all()
            if obj.__class__.__name__ == "Grade":
                return obj.student.school_class in teacher.classes.all()
            if obj.__class__.__name__ == "Teacher":
                return obj == teacher

        return False
class IsTeacherOrAdminCanEditComment(permissions.BasePermission):
    """
    - Un enseignant peut créer/modifier un commentaire UNIQUEMENT pour ses élèves,
      dans une matière et une classe qu’il enseigne.
    - L’admin a tous les droits.
    """

    def has_permission(self, request, view):
        if request.user.is_staff or request.user.is_superuser:
            return True
        return hasattr(request.user, "teacher")

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff or request.user.is_superuser:
            return True

        if hasattr(request.user, "teacher"):
            teacher = request.user.teacher
            return (
                obj.teacher == teacher
                and obj.student.school_class in teacher.classes.all()
                and obj.subject in teacher.subjects.all()
            )

        return False