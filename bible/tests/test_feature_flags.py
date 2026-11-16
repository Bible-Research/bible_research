"""Feature-flag helper, admin, and search-gate tests.

``sword_search`` is the SWORD-search kill switch: DB-backed,
admin-toggleable, read-through-cached, missing row = enabled.
"""
import pytest
from unittest.mock import MagicMock, patch
from django.contrib.admin.sites import AdminSite
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from bible.admin import FeatureFlagAdmin
from bible.models import FeatureFlag
from bible.services.feature_flags import (
    invalidate_feature_flag,
    is_sword_search_enabled,
)

SEARCH_URL = reverse('bible-search')
FLAG_KEY = 'sword_search'


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_missing_flag_row_defaults_enabled():
    """Kill-switch semantics: no row means the feature is on."""
    assert is_sword_search_enabled() is True


@pytest.mark.django_db
def test_enabled_row_returns_true():
    FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': True},
    )
    assert is_sword_search_enabled() is True


@pytest.mark.django_db
def test_disabled_row_returns_false():
    FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': False},
    )
    assert is_sword_search_enabled() is False


@pytest.mark.django_db
def test_flag_read_is_cached_until_invalidated():
    """The flag row is read once and cached: a row edit is
    invisible until the cache entry is invalidated."""
    flag, _ = FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': True},
    )
    assert is_sword_search_enabled() is True

    flag.enabled = False
    flag.save()
    # Still serving the stale cached value.
    assert is_sword_search_enabled() is True

    invalidate_feature_flag(FLAG_KEY)
    assert is_sword_search_enabled() is False


@pytest.mark.django_db
def test_admin_save_model_busts_flag_cache():
    """Toggling a flag via Django admin takes effect
    immediately — save_model drops the cached value."""
    flag, _ = FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': True},
    )
    assert is_sword_search_enabled() is True

    flag.enabled = False
    FeatureFlagAdmin(FeatureFlag, AdminSite()).save_model(
        MagicMock(), flag, form=None, change=True,
    )
    assert is_sword_search_enabled() is False


@pytest.mark.django_db
@patch('bible.services.providers.get_default_sword_client')
def test_disabled_flag_blocks_sword_grouped_search(
    mock_get_sword, client
):
    """``sword_search`` off → a grouped SWORD search 403s with
    ``search_disabled`` before any provider work runs."""
    FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': False},
    )

    response = client.get(SEARCH_URL, {
        'query': 'love',
        'fileset_id': 'LVSGLU8',
        'group_by': 'book',
    })

    assert response.status_code == status.HTTP_403_FORBIDDEN
    body = response.json()
    assert body['error_code'] == 'search_disabled'
    assert body['error']
    mock_get_sword.assert_not_called()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_sword_client')
def test_disabled_flag_blocks_paged_sword_search(
    mock_get_sword, client
):
    """The gate covers paginated mode too, not just
    ``group_by=book``."""
    FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': False},
    )

    response = client.get(SEARCH_URL, {
        'query': 'love',
        'fileset_id': 'LVSGLU8',
    })

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()['error_code'] == 'search_disabled'
    mock_get_sword.assert_not_called()


@pytest.mark.django_db
@patch('bible.services.providers.is_sword_fileset', return_value=False)
@patch('bible.services.providers.get_default_dbt_client')
def test_disabled_flag_does_not_gate_dbt(
    mock_get_client, mock_is_sword, client
):
    """The flag gates SWORD ids only — a DBT fileset searches
    normally while ``sword_search`` is off."""
    FeatureFlag.objects.update_or_create(
        key=FLAG_KEY, defaults={'enabled': False},
    )
    mock_get_client.return_value.search.return_value = {
        'verses': {'data': []},
        'meta': {'pagination': {'total': 0}},
    }

    response = client.get(SEARCH_URL, {
        'query': 'love',
        'fileset_id': 'ENGESV',
    })

    assert response.status_code == status.HTTP_200_OK
