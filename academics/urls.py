# academics/urls.py
from django.urls import path, include
from rest_framework import routers

from .views import (
    # ViewSets (CRUD standard)
    LevelViewSet,
    SchoolClassViewSet,
    SubjectViewSet,
    ClassSubjectViewSet,
    StudentViewSet,
    ParentViewSet,
    GradeViewSet,
    AnnouncementViewSet,
    TimetableViewSet,
    SubjectCommentViewSet,
    TimeSlotViewSet,
    ReportCardViewSet,
    StudentAttendanceViewSet,  # <-- Ajouté pour gérer les actions POST/DELETE des absences

    # APIViews (Actions spécifiques)
    GenerateTimetableView,
    TimetableConflictsView,
    ScheduleCheckView,
    TimetableBatchValidateView,
    TimetableBatchApplyView,
    DailyAttendanceSheetView,  # <-- Ajouté pour charger la feuille de présence
)

# Configuration du routeur
router = routers.DefaultRouter()

# Ressources académiques de base
router.register(r'levels', LevelViewSet, basename='level')
router.register(r'school-classes', SchoolClassViewSet, basename='schoolclass')
router.register(r'subjects', SubjectViewSet, basename='subject')
router.register(r'class-subjects', ClassSubjectViewSet, basename='classsubject')
router.register(r'time-slots', TimeSlotViewSet, basename="timeslot")

# Acteurs
router.register(r'students', StudentViewSet, basename='student')
router.register(r'parents', ParentViewSet, basename='parent')

# Gestion scolaire
router.register(r'grades', GradeViewSet, basename='grade')
router.register(r'report-cards', ReportCardViewSet, basename='report-card')
router.register(r'announcements', AnnouncementViewSet, basename='announcements')
router.register(r"subject-comments", SubjectCommentViewSet, basename="subjectcomment")

# Emploi du temps & Présences
router.register(r'timetable', TimetableViewSet, basename='timetable')
router.register(r'attendances', StudentAttendanceViewSet, basename='attendance') # <-- Route pour sauver les absences

urlpatterns = [
    # Routes du routeur (API REST standard)
    path('', include(router.urls)),

    # Routes spécifiques pour l'emploi du temps
    path('generate-timetable/', GenerateTimetableView.as_view(), name='generate-timetable'),
    path('timetable-conflicts/', TimetableConflictsView.as_view(), name='timetable-conflicts'),
    path('schedule-check/', ScheduleCheckView.as_view(), name='schedule-check'),
    path('timetable-batch-validate/', TimetableBatchValidateView.as_view(), name='timetable-batch-validate'),
    path('timetable-batch-apply/', TimetableBatchApplyView.as_view(), name='timetable-batch-apply'),

    # Route spécifique pour la feuille de présence (Récupération des données pour le tableau)
    path('attendance/sheet/', DailyAttendanceSheetView.as_view(), name='attendance-sheet'),
]