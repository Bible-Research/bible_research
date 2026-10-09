import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.views import AudioTimestampView


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
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


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
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
@patch('bible.views.get_default_dbt_client')
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
@patch('bible.views.get_default_dbt_client')
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
