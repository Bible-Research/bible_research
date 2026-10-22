from django.contrib import admin
from django.urls import path, include
from django.views.generic.base import RedirectView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny

from users.views import ThrottledObtainAuthToken

urlpatterns = [
    path('', RedirectView.as_view(url='/api/v1/docs/', permanent=False)),
    path('admin/', admin.site.urls),
    path('api/v1/', include('bible.urls')),
    path('api/v1/', include('annotations.urls')),
    path('api/v1/users/', include('users.urls')),
    path(
        'api/token/',
        ThrottledObtainAuthToken.as_view(),
        name='api_token',
    ),

    # API Schema and Documentation (public — auth enforced per endpoint)
    path(
        'api/v1/schema/',
        SpectacularAPIView.as_view(permission_classes=[AllowAny]),
        name='schema'
    ),
    path(
        'api/v1/docs/',
        SpectacularSwaggerView.as_view(
            url_name='schema',
            permission_classes=[AllowAny],
        ),
        name='swagger-ui'
    ),
    path(
        'api/v1/redoc/',
        SpectacularRedocView.as_view(
            url_name='schema',
            permission_classes=[AllowAny],
        ),
        name='redoc'
    ),
]
