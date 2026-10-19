from django.urls import path
from . import views

urlpatterns = [
    path('bible/', views.BiblePassageView.as_view(), name='bible-passage'),
    path(
        'bible/translations/',
        views.TranslationListView.as_view(),
        name='translation-list'
    ),
    path(
        'bible/languages/',
        views.LanguageListView.as_view(),
        name='language-list'
    ),
    path(
        'bible/timestamps/',
        views.AudioTimestampView.as_view(),
        name='audio-timestamps'
    ),
    path(
        'bible/copyright/',
        views.CopyrightView.as_view(),
        name='bible-copyright'
    ),
    path(
        'bible/search/',
        views.BibleSearchView.as_view(),
        name='bible-search'
    ),
]
