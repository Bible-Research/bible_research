import json

import pytest
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from google.api_core import exceptions as gcs_exceptions
from rest_framework import status
from rest_framework.test import APIClient, APIRequestFactory

from bible.services.apibible.registry import APIBIBLE_TRANSLATIONS
from bible.views import AudioTimestampView

AUDIO_BIBLE_ID = (
    APIBIBLE_TRANSLATIONS['ENGNIV_API']['audio_bible_id']
)


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.views.get_dbt_book_id')
def test_get_timestamps_success(
    mock_get_dbt_book_id, mock_get_client, factory
):
    """Test successful retrieval of timestamps."""
    mock_get_dbt_book_id.return_value = 'JHN'
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_timestamps.return_value = {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 5.5},
        ]
    }

    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'John',
        'chapter': '1'
    })
    view = AudioTimestampView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_200_OK
    assert len(response.data['data']) == 2
    assert response.data['data'][0]['verse_start'] == 1
    mock_get_dbt_book_id.assert_called_once_with('John')
    mock_dbt_instance.get_timestamps.assert_called_once_with(
        'ENGESVN2DA', 'JHN', 1
    )


@pytest.mark.django_db
def test_get_timestamps_missing_params(factory):
    """Test request with missing query parameters."""
    request = factory.get(
        '/fake-url/', {'book': 'John', 'chapter': '1'}
    )
    view = AudioTimestampView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'error' in response.data
    assert (
        response.data['error_code'] == 'missing_parameter'
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'fileset_id',
    ['evil/../../x', 'id with space', 'a' * 41, "bad'id"],
    ids=['path-traversal', 'spaces', 'overlong', 'quote'],
)
def test_get_timestamps_invalid_fileset_id(factory, fileset_id):
    """Garbage fileset ids 400 at the view boundary — before
    provider routing or upstream URL interpolation (S12)."""
    request = factory.get('/fake-url/', {
        'fileset_id': fileset_id,
        'book': 'John',
        'chapter': '1',
    })
    response = AudioTimestampView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        response.data['error_code'] == 'invalid_parameter'
    )


@pytest.mark.django_db
def test_get_timestamps_invalid_book_charset(factory):
    """A ``book`` value outside the name charset 400s before the
    ``get_dbt_book_id`` lookup."""
    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'John;drop table',
        'chapter': '1',
    })
    response = AudioTimestampView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        response.data['error_code'] == 'invalid_parameter'
    )


@pytest.mark.django_db
def test_global_anon_throttle_applies(factory):
    """The global ``AnonRateThrottle`` covers every view without
    its own ``throttle_classes`` — the baseline guard for
    anonymous traffic (S2)."""
    from django.core.cache import cache
    from rest_framework.throttling import AnonRateThrottle

    cache.clear()
    view = AudioTimestampView.as_view()
    with patch.dict(
        AnonRateThrottle.THROTTLE_RATES, {'anon': '2/min'}
    ):
        for _ in range(2):
            response = view(factory.get('/fake-url/', {}))
            assert (
                response.status_code
                == status.HTTP_400_BAD_REQUEST
            )
        response = view(factory.get('/fake-url/', {}))
    assert (
        response.status_code
        == status.HTTP_429_TOO_MANY_REQUESTS
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.views.get_dbt_book_id')
def test_get_timestamps_dbt_exception(
    mock_get_dbt_book_id, mock_get_client, factory
):
    """A DBT failure surfaces provider error fields, not str(exc)."""
    mock_get_dbt_book_id.return_value = 'JHN'
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_timestamps.side_effect = Exception(
        "DBT Error"
    )

    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'John',
        'chapter': '1'
    })
    view = AudioTimestampView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'DBT Error' not in response.data['error']


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.views.get_dbt_book_id')
def test_get_timestamps_rate_limited(
    mock_get_dbt_book_id, mock_get_client, factory
):
    """An upstream 429 maps to HTTP 429 + rate_limited."""
    mock_get_dbt_book_id.return_value = 'JHN'
    mock_get_client.return_value.get_timestamps.side_effect = (
        FakeProviderError(429)
    )

    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'John',
        'chapter': '1'
    })
    response = AudioTimestampView.as_view()(request)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.data['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.views.get_dbt_book_id')
def test_get_timestamps_error_does_not_leak_credentials(
    mock_get_dbt_book_id, mock_get_client, factory
):
    """Network errors stringify with the request URL — including
    DBT's ``?key=`` credential — so the raw exception text must
    never reach the response body."""
    mock_get_dbt_book_id.return_value = 'JHN'
    mock_get_client.return_value.get_timestamps.side_effect = (
        ConnectionError(
            'Max retries exceeded with url: '
            '/timestamps?key=SECRET'
        )
    )

    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'John',
        'chapter': '1'
    })
    response = AudioTimestampView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'SECRET' not in response.data['error']


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
@patch('bible.views.get_dbt_book_id')
def test_apibible_ranged_timecode_verse_ids(
    mock_get_dbt_book_id, mock_get_client, factory
):
    """Both ranged ``verseId`` shapes — ``JHN.3.16-17`` and the
    orgId form ``JHN.3.16-JHN.3.17`` — must resolve to the
    range's first verse, not its tail."""
    mock_get_dbt_book_id.return_value = 'JHN'
    mock_client = mock_get_client.return_value
    mock_client.get_chapter_audio.return_value = {
        "timecodes": [
            {"verseId": "JHN.3.16-17", "timestamp": "5.0"},
            {
                "verseId": "JHN.3.16-JHN.3.17",
                "timestamp": "5.0",
            },
        ]
    }

    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGNIVC1DA',
        'book': 'John',
        'chapter': '3'
    })
    response = AudioTimestampView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert [
        tc['verse_start'] for tc in response.data['data']
    ] == [16, 16]


@pytest.mark.django_db
def test_get_timestamps_unknown_book(factory):
    """Test request with an unknown book name."""
    request = factory.get('/fake-url/', {
        'fileset_id': 'ENGESVN2DA',
        'book': 'UnknownBook',
        'chapter': '1'
    })
    view = AudioTimestampView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'Unknown book' in response.data['error']


# ------------------------------------------------------------------
# Real-URL tests: APIClient + reverse('audio-timestamps')
# ------------------------------------------------------------------


@pytest.fixture
def auth_client(db):
    user, _ = get_user_model().objects.get_or_create(
        username='tsview'
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
@patch('bible.services.providers.get_tts_config')
@patch('bible.services.providers.gcs.read_timestamps_json')
def test_gcs_not_found_returns_404(
    mock_read_ts, mock_tts_config, auth_client
):
    """A missing GCS timestamps object is a 404 — the most likely
    real failure mode for freshly generated audio. The body
    carries the full ``error``/``error_code`` contract the
    React app branches on.
    """
    mock_tts_config.return_value = {
        'voice_name': 'lv-LV-Chirp3-HD-Sadachbia',
    }
    mock_read_ts.side_effect = gcs_exceptions.NotFound('missing')

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'LVSGLU8C1DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert set(body) == {'error', 'error_code'}
    assert body['error_code'] == 'not_found'
    assert body['error'] == (
        'Timestamps not yet generated for this chapter.'
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'audio_payload',
    [
        {'audio_url': 'https://x/JHN.3.mp3', 'timecodes': []},
        {'audio_url': 'https://x/JHN.3.mp3'},
    ],
    ids=['empty-timecodes', 'missing-timecodes'],
)
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_empty_timecodes_returns_404(
    mock_get_client, auth_client, audio_payload
):
    """An API.Bible audio chapter with no usable timecodes gets the
    same 'not yet generated' 404 as a missing GCS artifact —
    with the full ``error``/``error_code`` contract.
    """
    mock_client = mock_get_client.return_value
    mock_client.get_chapter_audio.return_value = audio_payload

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGNIVC1DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert set(body) == {'error', 'error_code'}
    assert body['error_code'] == 'not_found'
    assert body['error'] == (
        'Timestamps not yet generated for this chapter.'
    )
    mock_client.get_chapter_audio.assert_called_once_with(
        AUDIO_BIBLE_ID, 'JHN', 3
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_null_data_returns_empty_list(
    mock_get_client, auth_client
):
    """DBT answers ``{"data": null}`` for chapters it knows
    but has no timestamps for — that must produce an empty
    ``data`` list, not a TypeError → 502."""
    mock_get_client.return_value.get_timestamps.return_value = {
        "data": None
    }

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGESVN2DA',
            'book': 'John',
            'chapter': '1',
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {'data': []}


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_timecodes_normalized(
    mock_get_client, auth_client
):
    """API.Bible timecodes are reshaped by ``_apibible_timecode``
    into ``{verse_start, timestamp}`` with real int/float
    values — covering both the ``verseId`` and ``verse_start``
    source fields."""
    mock_client = mock_get_client.return_value
    mock_client.get_chapter_audio.return_value = {
        'audio_url': 'https://x/JHN.3.mp3',
        'timecodes': [
            {'verseId': 'JHN.3.16', 'timestamp': '5.5'},
            {'verse_start': '17', 'timestamp': '9'},
        ],
    }

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGNIVC1DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body['data'] == [
        {'verse_start': 16, 'timestamp': 5.5},
        {'verse_start': 17, 'timestamp': 9.0},
    ]


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_upstream_404_maps_to_not_found(
    mock_get_client, auth_client
):
    """An upstream HTTP 404 from the DBT timestamps call reaches
    the outer ``provider_error_response`` → 404 ``not_found``
    (the full error contract, unlike the bare-error branches)."""
    mock_get_client.return_value.get_timestamps.side_effect = (
        FakeProviderError(404)
    )

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGESVN2DA',
            'book': 'John',
            'chapter': '1',
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    body = response.json()
    assert body['error_code'] == 'not_found'
    assert body['error'] == 'Passage not found (HTTP 404)'


@pytest.mark.django_db
@patch('bible.services.providers.get_tts_config')
@patch('bible.services.providers.gcs.read_timestamps_json')
def test_read_timestamps_json_positional_args(
    mock_read_ts, mock_tts_config, auth_client
):
    """The abbr ``GLU8`` must canonicalize to ``LVSGLU8`` before
    the GCS lookup — passing ``GLU8`` through would read a
    nonexistent artifact prefix. ``mode`` stays ``'verse'`` for
    non-chapter fileset ids."""
    mock_tts_config.return_value = {
        'voice_name': 'lv-LV-Chirp3-HD-Sadachbia',
    }
    mock_read_ts.return_value = {'data': []}

    response = auth_client.get(
        reverse('audio-timestamps'),
        {'fileset_id': 'GLU8', 'book': 'John', 'chapter': '3'},
    )

    assert response.status_code == status.HTTP_200_OK
    mock_tts_config.assert_called_once_with('LVSGLU8')
    mock_read_ts.assert_called_once_with(
        'LVSGLU8', 'JHN', 3,
        'lv-LV-Chirp3-HD-Sadachbia',
        mode='verse',
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_tts_config')
@patch('bible.services.providers.gcs.read_timestamps_json')
def test_gcs_payload_strips_size_fields(
    mock_read_ts, mock_tts_config, auth_client
):
    """Only the payload's ``data`` list is relayed — generator
    bookkeeping fields (``duration_seconds``,
    ``file_size_bytes``) must not leak into the response body."""
    mock_tts_config.return_value = {
        'voice_name': 'lv-LV-Chirp3-HD-Sadachbia',
    }
    mock_read_ts.return_value = {
        'duration_seconds': 60.0,
        'file_size_bytes': 1234567,
        'data': [
            {'verse_start': 1, 'timestamp': 0.0},
            {'verse_start': 2, 'timestamp': 5.34},
        ],
    }

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'LVSGLU8C1DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {'data'}
    assert body['data'] == [
        {'verse_start': 1, 'timestamp': 0.0},
        {'verse_start': 2, 'timestamp': 5.34},
    ]


# --- timestamps JSON cache (S7 / timestamps #5) ------------------
#
# ``read_timestamps_json`` caches the immutable artifact payload in
# the Django cache; both the timestamps endpoint and the passage
# audio path share the entry. A cache hit must not reach GCS.


def _gcs_blob_mock(payload):
    """Mock ``get_default_client`` so ``blob.download_as_bytes``
    returns ``payload`` encoded — returns (client_mock, blob)."""
    blob = MagicMock()
    blob.download_as_bytes.return_value = (
        json.dumps(payload).encode()
    )
    client = MagicMock()
    client.bucket.return_value.blob.return_value = blob
    return client, blob


@pytest.mark.django_db
@patch('bible.services.storage.gcs.get_default_client')
def test_timestamps_json_second_request_hits_cache(
    mock_get_gcs, auth_client
):
    """Two identical requests download the artifact once — the
    second response comes from the Django cache (S7).

    The real ``get_tts_config`` registry resolves the voice —
    no SWORD/GCS patching needed beyond the storage client."""
    client_mock, blob = _gcs_blob_mock({
        'data': [{'verse_start': 1, 'timestamp': 0.0}],
    })
    mock_get_gcs.return_value = client_mock
    params = {
        'fileset_id': 'LVSGLU8C1DA',
        'book': 'John',
        'chapter': '3',
    }

    first = auth_client.get(reverse('audio-timestamps'), params)
    second = auth_client.get(reverse('audio-timestamps'), params)

    assert first.status_code == status.HTTP_200_OK
    assert second.json() == first.json()
    blob.download_as_bytes.assert_called_once()


@pytest.mark.django_db
@patch('bible.services.storage.gcs.get_default_client')
def test_timestamps_json_cache_key_covers_chapter_and_mode(
    mock_get_gcs, auth_client
):
    """``chapter`` and ``mode`` are part of the cache key:
    another chapter (or the C2DA chapter-mode fileset) is a
    separate artifact download."""
    client_mock, blob = _gcs_blob_mock({'data': []})
    mock_get_gcs.return_value = client_mock

    auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'LVSGLU8C1DA',
            'book': 'John',
            'chapter': '3',
        },
    )
    auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'LVSGLU8C1DA',
            'book': 'John',
            'chapter': '4',
        },
    )
    auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'LVSGLU8C2DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert blob.download_as_bytes.call_count == 3


@pytest.mark.django_db
@patch('bible.services.storage.gcs.get_default_client')
def test_timestamps_json_miss_not_cached(
    mock_get_gcs, auth_client
):
    """A missing artifact (NotFound) must not be cached — the
    next request re-checks GCS so freshly generated chapters
    appear immediately."""
    client_mock, blob = _gcs_blob_mock({'data': []})
    blob.download_as_bytes.side_effect = [
        gcs_exceptions.NotFound('missing'),
        b'{"data": [{"verse_start": 1, "timestamp": 0.0}]}',
    ]
    mock_get_gcs.return_value = client_mock
    params = {
        'fileset_id': 'LVSGLU8C1DA',
        'book': 'John',
        'chapter': '3',
    }

    missing = auth_client.get(
        reverse('audio-timestamps'), params
    )
    generated = auth_client.get(
        reverse('audio-timestamps'), params
    )

    assert missing.status_code == status.HTTP_404_NOT_FOUND
    assert generated.status_code == status.HTTP_200_OK
    assert blob.download_as_bytes.call_count == 2


# --- API.Bible timecode filtering (timestamps #8) ----------------


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_unparseable_timecodes_filtered(
    mock_get_client, auth_client
):
    """Malformed timecodes are dropped instead of emitting
    ``null`` verse/timestamp fields; a fully unparseable list
    answers the same 'not yet generated' 404 as an empty one."""
    mock_client = mock_get_client.return_value
    mock_client.get_chapter_audio.return_value = {
        'audio_url': 'https://x/JHN.3.mp3',
        'timecodes': [
            {'verseId': 'JHN.3.16', 'timestamp': '5.5'},
            {'verseId': 'bogus', 'timestamp': '9'},
            {'verseId': 'JHN.3.18', 'timestamp': 'not-a-float'},
            {'verseId': 'JHN.3.19', 'timestamp': '12'},
        ],
    }

    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGNIVC1DA',
            'book': 'John',
            'chapter': '3',
        },
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['data'] == [
        {'verse_start': 16, 'timestamp': 5.5},
        {'verse_start': 19, 'timestamp': 12.0},
    ]

    mock_client.get_chapter_audio.return_value = {
        'timecodes': [{'verseId': 'bogus', 'timestamp': 'x'}],
    }
    response = auth_client.get(
        reverse('audio-timestamps'),
        {
            'fileset_id': 'ENGNIVC1DA',
            'book': 'John',
            'chapter': '3',
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()['error_code'] == 'not_found'
