# academics/urls.py
# =============================================================================
#  REMPLACER ENTIÈREMENT l'ancien fichier academics/urls.py par celui-ci.
#  Ce fichier enregistre TOUS les viewsets incluant les trois nouveaux :
#    - SchoolYearConfigViewSet   → /api/academics/school-year-config/
#    - TermSubjectConfigViewSet  → /api/academics/term-subject-configs/
#    - TermStatusViewSet         → /api/academics/term-status/
# =============================================================================

from django.urls import path, include
from rest_framework import routers

from .views import (
    # Académique de base
    LevelViewSet,
    SchoolClassViewSet,
    SubjectViewSet,
    ClassSubjectViewSet,
    TimeSlotViewSet,
    # Acteurs
    StudentViewSet,
    ParentViewSet,
    UserViewSet,
    # Notes et bulletins
    GradeViewSet,
    DraftGradeViewSet,
    ReportCardViewSet,
    SubjectCommentViewSet,
    # Emploi du temps
    TimetableViewSet,
    ClassScheduleEntryViewSet,
    # Présences
    AttendanceSessionViewSet,
    StudentAttendanceViewSet,
    # Annonces
    AnnouncementViewSet,
    # Cycle de vie des trimestres (NOUVEAUX)
    SchoolYearConfigViewSet,
    TermSubjectConfigViewSet,
    TermStatusViewSet,
    # APIViews
    GenerateTimetableView,
    TimetableConflictsView,
    ScheduleCheckView,
    TimetableBatchValidateView,
    TimetableBatchApplyView,
    DailyAttendanceSheetView,
    StudentAttendanceHistoryView,
    CopyClassConfigView,
)

router = routers.DefaultRouter()

# ── Ressources de base ────────────────────────────────────────────────────────
router.register(r"users",           UserViewSet,          basename="user")
router.register(r"levels",          LevelViewSet,         basename="level")
router.register(r"school-classes",  SchoolClassViewSet,   basename="schoolclass")
router.register(r"subjects",        SubjectViewSet,       basename="subject")
router.register(r"class-subjects",  ClassSubjectViewSet,  basename="classsubject")
router.register(r"time-slots",      TimeSlotViewSet,      basename="timeslot")

# ── Acteurs ───────────────────────────────────────────────────────────────────
router.register(r"students", StudentViewSet, basename="student")
router.register(r"parents",  ParentViewSet,  basename="parent")

# ── Notes et bulletins ────────────────────────────────────────────────────────
router.register(r"grades",           GradeViewSet,          basename="grade")
router.register(r"draft-grades",     DraftGradeViewSet,     basename="draftgrade")
router.register(r"report-cards",     ReportCardViewSet,     basename="report-card")
router.register(r"subject-comments", SubjectCommentViewSet, basename="subjectcomment")

# ── Emploi du temps ───────────────────────────────────────────────────────────
router.register(r"timetable",         TimetableViewSet,         basename="timetable")
router.register(r"schedule-entries",  ClassScheduleEntryViewSet, basename="schedule-entry")

# ── Présences ─────────────────────────────────────────────────────────────────
router.register(r"attendance/sessions", AttendanceSessionViewSet, basename="attendance-session")
router.register(r"attendance/absences", StudentAttendanceViewSet, basename="student-attendance")

# ── Annonces ──────────────────────────────────────────────────────────────────
router.register(r"announcements", AnnouncementViewSet, basename="announcements")

# ── Configuration de l'année scolaire (singleton) ────────────────────────────
#   GET    /api/academics/school-year-config/
#   GET    /api/academics/school-year-config/1/
#   PUT    /api/academics/school-year-config/1/
#   PATCH  /api/academics/school-year-config/1/
router.register(r"school-year-config", SchoolYearConfigViewSet, basename="school-year-config")

# ── Configuration pédagogique par matière × classe × trimestre ───────────────
#   GET    /api/academics/term-subject-configs/?school_class=3&term=T1
#   POST   /api/academics/term-subject-configs/
#   GET    /api/academics/term-subject-configs/{id}/
#   PATCH  /api/academics/term-subject-configs/{id}/
#   DELETE /api/academics/term-subject-configs/{id}/
#   POST   /api/academics/term-subject-configs/bulk/  ← sauvegarde en masse
router.register(r"term-subject-configs", TermSubjectConfigViewSet, basename="term-subject-config")

# ── Cycle de vie des trimestres par classe ────────────────────────────────────
#   GET    /api/academics/term-status/?school_class=3&term=T1
#   POST   /api/academics/term-status/
#   GET    /api/academics/term-status/{id}/
#   DELETE /api/academics/term-status/{id}/       (admin, DRAFT seulement)
#   POST   /api/academics/term-status/{id}/lock/
#   POST   /api/academics/term-status/{id}/unlock/
#   POST   /api/academics/term-status/{id}/publish/
#   POST   /api/academics/term-status/{id}/unpublish/
router.register(r"term-status", TermStatusViewSet, basename="term-status")


urlpatterns = [
    path("", include(router.urls)),

    # ── Emploi du temps ───────────────────────────────────────────────────────
    path("generate-timetable/",       GenerateTimetableView.as_view(),      name="generate-timetable"),
    path("timetable-conflicts/",      TimetableConflictsView.as_view(),     name="timetable-conflicts"),
    path("schedule-check/",           ScheduleCheckView.as_view(),          name="schedule-check"),
    path("timetable-batch-validate/", TimetableBatchValidateView.as_view(), name="timetable-batch-validate"),
    path("timetable-batch-apply/",    TimetableBatchApplyView.as_view(),    name="timetable-batch-apply"),

    # ── Présences ─────────────────────────────────────────────────────────────
    path("attendance/daily-sheet/", DailyAttendanceSheetView.as_view(),     name="attendance-daily-sheet"),
    path("attendance/history/",     StudentAttendanceHistoryView.as_view(), name="attendance-history"),

    # ── Utilitaires ───────────────────────────────────────────────────────────
    path("class-subjects/copy-config/", CopyClassConfigView.as_view(), name="copy-class-config"),
]