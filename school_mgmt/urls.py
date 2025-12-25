from django.contrib import admin
from django.urls import path, include, re_path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

# Imports pour servir les fichiers médias
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve 

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Tes routes API
    path('api/core/', include('core.urls')),           # Parents et élèves
    path('api/academics/', include('academics.urls')), # Niveaux, classes, matières, notes
    path('api/fees/', include('fees.urls')),           # Frais de scolarité
    path('api/notifications/', include('notifications.urls')), # Notifications

    # Authentification JWT
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

# -----------------------------------------------------------
# GESTION DES FICHIERS STATIQUES ET MEDIAS
# -----------------------------------------------------------

if settings.DEBUG:
    # En Développement : Django sert tout automatiquement
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

else:
    # En Production (Railway) :
    # Puisque nous n'avons pas Nginx devant pour servir les médias du volume,
    # nous forçons Django à le faire via la vue 'serve'.
    # Note : WhiteNoise gère déjà les fichiers STATIC, mais pas les MEDIA.
    
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {
            'document_root': settings.MEDIA_ROOT,
        }),
    ]