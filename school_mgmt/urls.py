from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/core/', include('core.urls')),            # Parents et élèves
    path('api/academics/', include('academics.urls')),  # Niveaux, classes, matières, notes, bulletins
    path('api/fees/', include('fees.urls')),            # Frais de scolarité
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]
