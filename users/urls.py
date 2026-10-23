from django.urls import path

from .views import CurrentUserView, register_user

urlpatterns = [
    path('register/', register_user, name='user-register'),
    path('me/', CurrentUserView.as_view(), name='current-user'),
]
