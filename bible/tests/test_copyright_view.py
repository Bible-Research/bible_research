import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.views import CopyrightView


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


@pytest.fixture
def factory():
    return APIRequestFactory()


SAMPLE_COPYRIGHT_DATA = [
    {
        "id": "ENGESV",
        "type": "text_plain",
        "size": "C",
        "copyright": {
            "copyright_date": "2001",
            "copyright": "© 2001 Crossway Bibles",
            "copyright_description": (
                "The Holy Bible, English Standard Version"
            ),
            "open_access": 0,
        },
    },
    {
        "id": "ENGESVN2DA",
        "type": "audio",
        "size": "NT",
        "copyright": {
            "copyright_date": "2001",
            "copyright": "© 2001 Crossway Audio",
            "copyright_description": "ESV Audio",
            "open_access": 0,
        },
    },
]


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_copyright_success(mock_get_client, factory):
    """Test successful retrieval of copyright info."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_copyright.return_value = (
        SAMPLE_COPYRIGHT_DATA
    )

    request = factory.get(
        '/fake-url/', {'bible_id': 'ENGESV'}
    )
    view = CopyrightView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_200_OK
    assert len(response.data['data']) == 2

    text_cr = response.data['data'][0]
    assert text_cr['id'] == 'ENGESV'
    assert text_cr['type'] == 'text_plain'
    assert text_cr['copyright'] == (
        '© 2001 Crossway Bibles'
    )
    assert text_cr['copyright_date'] == '2001'
    assert text_cr['copyright_description'] == (
        'The Holy Bible, English Standard Version'
    )

    mock_dbt_instance.get_copyright \
        .assert_called_once_with('ENGESV')


@pytest.mark.django_db
def test_get_copyright_missing_bible_id(factory):
    """Test request with missing bible_id parameter."""
    request = factory.get('/fake-url/')
    view = CopyrightView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'error' in response.data


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_copyright_dbt_exception(
    mock_get_client, factory
):
    """Test handling of an exception from DBT client."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_copyright.side_effect = (
        Exception("DBT Error")
    )

    request = factory.get(
        '/fake-url/', {'bible_id': 'ENGESV'}
    )
    view = CopyrightView.as_view()
    response = view(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'DBT Error' not in response.data['error']


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_copyright_rate_limited(mock_get_client, factory):
    """An upstream 429 maps to HTTP 429 + rate_limited."""
    mock_get_client.return_value.get_copyright.side_effect = (
        FakeProviderError(429)
    )

    request = factory.get(
        '/fake-url/', {'bible_id': 'ENGESV'}
    )
    response = CopyrightView.as_view()(request)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.data['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_copyright_error_does_not_leak_credentials(
    mock_get_client, factory
):
    """Exception text can carry ``?key=`` credentials — it must
    never reach the response body."""
    mock_get_client.return_value.get_copyright.side_effect = (
        ConnectionError(
            'Max retries exceeded with url: '
            '/copyright?key=SECRET'
        )
    )

    request = factory.get(
        '/fake-url/', {'bible_id': 'ENGESV'}
    )
    response = CopyrightView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'SECRET' not in response.data['error']
