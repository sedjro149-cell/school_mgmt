# finance/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import FeeViewSet, FeeTypeViewSet, PaymentViewSet, FeeTypeAmountViewSet, fees_statistics, fees_monthly

router = DefaultRouter()
router.register(r"fee-types", FeeTypeViewSet, basename="fee-type")
router.register(r"fee-type-amounts", FeeTypeAmountViewSet, basename="fee-type-amount")
router.register(r"fees", FeeViewSet, basename="fee")
router.register(r"payments", PaymentViewSet, basename="payment")

urlpatterns = [
    path("", include(router.urls)),
    path("statistics/", fees_statistics, name="fees-statistics"),
    path("statistics/monthly/", fees_monthly, name="fees-statistics-monthly"),
]
