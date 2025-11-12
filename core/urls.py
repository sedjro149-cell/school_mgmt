from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ParentViewSet,
    StudentViewSet,
    TeacherViewSet,
    ProfileView,
    ParentRegisterView,
    StudentRegisterView,
    TeacherRegisterView,
    DashboardStatsView,
    DashboardTopStudentsView
)

router = DefaultRouter()
# CRUD sécurisé pour l'admin
router.register(r'admin/parents', ParentViewSet)
router.register(r'admin/students', StudentViewSet)
router.register(r'admin/teachers', TeacherViewSet)

urlpatterns = [
    # Routes CRUD sécurisées via router
    path('', include(router.urls)),

    # Profil connecté
    path('me/', ProfileView.as_view(), name='profile'),

    # Routes publiques pour inscription
    path('register/parents/', ParentRegisterView.as_view(), name='register-parent'),
    path('register/students/', StudentRegisterView.as_view(), name='register-student'),
    path('register/teachers/', TeacherRegisterView.as_view(), name='register-teacher'),
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
   path('dashboard/top-students/', DashboardTopStudentsView.as_view(), name='dashboard-top-students'),
]
