# academics/urls.py
from django.urls import path, include
from rest_framework import routers

# importe la viewset correcte
from .views import (
    LevelViewSet,
    SchoolClassViewSet,
    SubjectViewSet,
    ClassSubjectViewSet,
    StudentViewSet,
    ParentViewSet,
    GradeViewSet,
    # ClassScheduleEntryViewSet,  # <-- retire ou commente si tu veux désambiguïser
    TimetableViewSet,            # <-- utilise la viewset que nous avons modifiée
    SubjectCommentViewSet, #C:\Users\hp\school_mgmt>py -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
                    #186k#kc-u5w$s35_0)1@!fvbk@y-5!k@w4b&mjcwre39zf_sf%
    TimeSlotViewSet,
    GenerateTimetableView,
    TimetableConflictsView,
)
from academics.views import ReportCardViewSet

router = routers.DefaultRouter()
router.register(r'levels', LevelViewSet, basename='level')
router.register(r'school-classes', SchoolClassViewSet, basename='schoolclass')
router.register(r'subjects', SubjectViewSet, basename='subject')
router.register(r'class-subjects', ClassSubjectViewSet, basename='classsubject')
router.register(r'students', StudentViewSet, basename='student')
router.register(r'parents', ParentViewSet, basename='parent')
router.register(r'grades', GradeViewSet, basename='grade')

# <-- ici on enregistre la viewset 'TimetableViewSet' sous le préfixe 'timetable'
router.register(r'timetable', TimetableViewSet, basename='timetable')

# si tu veux garder l'ancienne ClassScheduleEntryViewSet mais sous un autre chemin:
# router.register(r'class-schedule-raw', ClassScheduleEntryViewSet, basename='classschedule-raw')

router.register(r'report-cards', ReportCardViewSet, basename='report-card')
router.register(r"subject-comments", SubjectCommentViewSet, basename="subjectcomment")
router.register(r"time-slots", TimeSlotViewSet, basename="timeslot")


urlpatterns = [
    path('', include(router.urls)),
    path('generate-timetable/', GenerateTimetableView.as_view(), name='generate-timetable'),
    path('timetable-conflicts/', TimetableConflictsView.as_view(), name='timetable-conflicts'),

]
