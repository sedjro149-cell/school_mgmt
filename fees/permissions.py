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

        # 1. Récupérer l'étudiant concerné par l'objet (Fee ou Payment)
        student = getattr(obj, "student", None)
        if student is None and hasattr(obj, "fee"):
             # Si c'est un Payment, on remonte via le Fee
            student = getattr(obj.fee, "student", None)

        if not student:
            return False

        # 2. Vérification pour l'Étudiant
        if hasattr(user, "student") and student == user.student:
            return request.method in permissions.SAFE_METHODS

        # 3. Vérification pour le Parent
        # Attention : Assure-toi que la relation student.parent est correcte (OneToOne ou ForeignKey).
        # Si c'est du ManyToMany, il faudra utiliser: user.parent in student.parents.all()
        if hasattr(user, "parent"):
            # On suppose ici que student.parent existe comme dans ton code original
            # On utilise 'student' récupéré plus haut au lieu de 'obj.student'
            parent_of_student = getattr(student, "parent", None)
            if parent_of_student == user.parent:
                return request.method in permissions.SAFE_METHODS
            
            # Alternative si relation ManyToMany (plus fréquent) :
            # if hasattr(student, "parents") and user.parent in student.parents.all():
            #    return request.method in permissions.SAFE_METHODS

        return False