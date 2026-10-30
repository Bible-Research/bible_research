"""Serving-path tests for the generated-timestamps fallback in
``DbtProvider.get_timestamps``: registered DBT filesets whose
DBT timestamps are empty (or whose call fails) read the
``timestamps_generated/`` artifact instead."""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from google.api_core import exceptions as gcs_exceptions
from rest_framework.test import APIClient

from bible.services.providers import DbtProvider


_PROVIDERS = "bible.services.providers"


@pytest.fixture
def auth_client(db):
    user, _ = get_user_model().objects.get_or_create(
        username="tsgen"
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_empty_dbt_data_reads_generated_artifact(
    get_dbt, read_gen,
):
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [],
    }
    read_gen.return_value = {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 5.5},
        ],
        "meta": {"source": "stt_v1"},
    }
    data = DbtProvider("LATLVRN1DA").get_timestamps("JHN", 3)
    assert data == [
        {"verse_start": 1, "timestamp": 0.0},
        {"verse_start": 2, "timestamp": 5.5},
    ]
    read_gen.assert_called_once_with("LATLVRN1DA", "JHN", 3)


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_empty_dbt_data_missing_artifact_returns_empty(
    get_dbt, read_gen,
):
    """The 200-empty contract is preserved when no artifact
    exists yet (the frontend's fileset fallback depends on it)."""
    get_dbt.return_value.get_timestamps.return_value = {
        "data": None,
    }
    read_gen.side_effect = gcs_exceptions.NotFound("missing")
    data = DbtProvider("LATLVRN1DA").get_timestamps("JHN", 3)
    assert data == []


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_dbt_exception_falls_back_to_artifact(get_dbt, read_gen):
    get_dbt.return_value.get_timestamps.side_effect = (
        RuntimeError("dbt down")
    )
    read_gen.return_value = {
        "data": [{"verse_start": 1, "timestamp": 0.0}],
    }
    data = DbtProvider("LATLVRN1DA").get_timestamps("JHN", 3)
    assert data == [{"verse_start": 1, "timestamp": 0.0}]


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_dbt_exception_reraises_without_artifact(
    get_dbt, read_gen,
):
    """When DBT fails and we have nothing better, today's 502
    behaviour is preserved — the DBT error propagates."""
    get_dbt.return_value.get_timestamps.side_effect = (
        RuntimeError("dbt down")
    )
    read_gen.side_effect = gcs_exceptions.NotFound("missing")
    with pytest.raises(RuntimeError):
        DbtProvider("LATLVRN1DA").get_timestamps("JHN", 3)


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_dbt_data_wins_over_artifact(get_dbt, read_gen):
    """Real DBT coverage always wins — generated artifacts are a
    fallback, never an override."""
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [{"verse_start": 1, "timestamp": 1.0}],
    }
    data = DbtProvider("LATLVRN1DA").get_timestamps("JHN", 3)
    assert data == [{"verse_start": 1, "timestamp": 1.0}]
    read_gen.assert_not_called()


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_opus16_id_reads_base_artifact(get_dbt, read_gen):
    """``-opus16`` variants are the same recordings — the
    fallback strips the suffix and reads the base fileset's
    artifact (the frontend does the same client-side)."""
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [],
    }
    read_gen.return_value = {
        "data": [{"verse_start": 1, "timestamp": 0.0}],
    }
    data = DbtProvider(
        "LATLVRN1DA-opus16"
    ).get_timestamps("JHN", 3)
    assert data == [{"verse_start": 1, "timestamp": 0.0}]
    read_gen.assert_called_once_with("LATLVRN1DA", "JHN", 3)


@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_unregistered_fileset_never_touches_gcs(
    get_dbt, read_gen,
):
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [],
    }
    data = DbtProvider("ENGESVN2DA").get_timestamps("JHN", 3)
    assert data == []
    read_gen.assert_not_called()


# ------------------------------------------------------------------
# End-to-end through the timestamps view (contract check)
# ------------------------------------------------------------------


@pytest.mark.django_db
@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_view_serves_generated_rows_for_registered_fileset(
    get_dbt, read_gen, auth_client,
):
    """``GET /api/v1/bible/timestamps/?fileset_id=LATLVRN1DA``
    returns the generated rows where it used to be ``[]``."""
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [],
    }
    read_gen.return_value = {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 4.25},
        ],
    }
    response = auth_client.get(
        reverse("audio-timestamps"),
        {
            "fileset_id": "LATLVRN1DA",
            "book": "John",
            "chapter": "3",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 4.25},
        ],
    }


@pytest.mark.django_db
@patch(f"{_PROVIDERS}.gcs.read_generated_timestamps_json")
@patch(f"{_PROVIDERS}.get_default_dbt_client")
def test_view_returns_empty_list_without_artifact(
    get_dbt, read_gen, auth_client,
):
    get_dbt.return_value.get_timestamps.return_value = {
        "data": [],
    }
    read_gen.side_effect = gcs_exceptions.NotFound("missing")
    response = auth_client.get(
        reverse("audio-timestamps"),
        {
            "fileset_id": "LATLVRN1DA",
            "book": "John",
            "chapter": "3",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"data": []}
