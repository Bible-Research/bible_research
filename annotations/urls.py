from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from .views_tasks import TranslateNoteTaskView

router = DefaultRouter()
router.register(r'tags', views.TagViewSet, basename='tag')
router.register(r'notes', views.NoteViewSet, basename='note')
router.register(
    r'reading-positions',
    views.ReadingPositionViewSet,
    basename='reading-position'
)
router.register(
    r'notes/(?P<note_pk>[^/.]+)/comments',
    views.CommentViewSet,
    basename='comment',
)
router.register(
    r'notes/(?P<note_pk>[^/.]+)/images',
    views.NoteImageViewSet,
    basename='note-image',
)
router.register(
    r'notes/(?P<note_pk>[^/.]+)/comments'
    r'/(?P<comment_pk>[^/.]+)/images',
    views.CommentImageViewSet,
    basename='comment-image',
)

urlpatterns = [
    path(
        'comments/counts/',
        views.CommentCountView.as_view(),
        name='comment-counts',
    ),
    path(
        'images/<str:image_pk>/',
        views.ImageDestroyView.as_view(),
        name='image-destroy',
    ),
    path(
        'tasks/translate-note/',
        TranslateNoteTaskView.as_view(),
        name='translate-note-task',
    ),
    path('', include(router.urls)),
]
