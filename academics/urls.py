from django.urls import path, include
from rest_framework import routers

from .views import (
    # ViewSets CRUD
    LevelViewSet,
    SchoolClassViewSet,
    SubjectViewSet,
    ClassSubjectViewSet,
    StudentViewSet,
    ParentViewSet,
    GradeViewSet,
    DraftGradeViewSet,
    ReportCardViewSet,
    AnnouncementViewSet,
    TimetableViewSet,
    SubjectCommentViewSet,
    TimeSlotViewSet,

    # Attendance
    AttendanceSessionViewSet,
    StudentAttendanceViewSet,

    # APIViews
    GenerateTimetableView,
    TimetableConflictsView,
    ScheduleCheckView,
    TimetableBatchValidateView,
    TimetableBatchApplyView,
    DailyAttendanceSheetView,
    StudentAttendanceHistoryView,
)

router = routers.DefaultRouter()

# Ressources academiques de base
router.register(r'levels',          LevelViewSet,         basename='level')
router.register(r'school-classes',  SchoolClassViewSet,   basename='schoolclass')
router.register(r'subjects',        SubjectViewSet,       basename='subject')
router.register(r'class-subjects',  ClassSubjectViewSet,  basename='classsubject')
router.register(r'time-slots',      TimeSlotViewSet,      basename='timeslot')

# Acteurs
router.register(r'students', StudentViewSet, basename='student')
router.register(r'parents',  ParentViewSet,  basename='parent')

# Gestion scolaire
router.register(r'grades',           GradeViewSet,          basename='grade')
router.register(r'draft-grades',     DraftGradeViewSet,     basename='draftgrade')
router.register(r'report-cards',     ReportCardViewSet,     basename='report-card')
router.register(r'announcements',    AnnouncementViewSet,   basename='announcements')
router.register(r'subject-comments', SubjectCommentViewSet, basename='subjectcomment')

# Emploi du temps
router.register(r'timetable', TimetableViewSet, basename='timetable')

# Presences
# /api/academics/attendance/sessions/           CRUD + submit, cancel, reopen, sheet
# /api/academics/attendance/sessions/{id}/submit/
# /api/academics/attendance/sessions/{id}/cancel/
# /api/academics/attendance/sessions/{id}/reopen/
# /api/academics/attendance/sessions/{id}/sheet/
# /api/academics/attendance/absences/           CRUD absences individuelles
router.register(r'attendance/sessions', AttendanceSessionViewSet, basename='attendance-session')
router.register(r'attendance/absences', StudentAttendanceViewSet, basename='student-attendance')

urlpatterns = [
    path('', include(router.urls)),

    # Emploi du temps
    path('generate-timetable/',       GenerateTimetableView.as_view(),      name='generate-timetable'),
    path('timetable-conflicts/',      TimetableConflictsView.as_view(),     name='timetable-conflicts'),
    path('schedule-check/',           ScheduleCheckView.as_view(),          name='schedule-check'),
    path('timetable-batch-validate/', TimetableBatchValidateView.as_view(), name='timetable-batch-validate'),
    path('timetable-batch-apply/',    TimetableBatchApplyView.as_view(),    name='timetable-batch-apply'),

    # Presences - vues non couvertes par le router
    path('attendance/daily-sheet/', DailyAttendanceSheetView.as_view(),     name='attendance-daily-sheet'),
    path('attendance/history/',     StudentAttendanceHistoryView.as_view(), name='attendance-history'),
]