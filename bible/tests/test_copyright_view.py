import pytest
from unittest.mock import patch
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from bible.services.apibible.registry import get_apibible_meta


COPYRIGHT_URL = reverse('bible-copyright')
APINIV_BIBLE_ID = get_apibible_meta('ENGNIV_API')['bible_id']


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


@pytest.fixture
def client():
    return APIClient()


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
@patch('bible.services.providers.get_default_dbt_client')
def test_get_copyright_success(mock_get_client, client):
    """Test successful retrieval of copyright info."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_copyright.return_value = (
        SAMPLE_COPYRIGHT_DATA
    )

    response = client.get(COPYRIGHT_URL, {'bible_id': 'ENGESV'})

    assert response.status_code == status.HTTP_200_OK
    data = response.json()['data']
    assert len(data) == 2

    text_cr = data[0]
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
def test_get_copyright_missing_bible_id(client):
    """Test request with missing bible_id parameter."""
    response = client.get(COPYRIGHT_URL)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    assert 'error' in body
    assert body['error_code'] == 'missing_parameter'


@pytest.mark.django_db
@pytest.mark.parametrize(
    'bible_id',
    ['evil/../../x', 'id with space', 'a' * 65, "bad'id"],
    ids=['path-traversal', 'spaces', 'overlong', 'quote'],
)
def test_get_copyright_invalid_bible_id(client, bible_id):
    """A bible_id outside the id charset 400s before any
    upstream URL is built (S12 / copyright #4)."""
    response = client.get(COPYRIGHT_URL, {'bible_id': bible_id})

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        response.json()['error_code'] == 'invalid_parameter'
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_get_copyright_dbt_exception(
    mock_get_client, client
):
    """Test handling of an exception from DBT client."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_copyright.side_effect = (
        Exception("DBT Error")
    )

    response = client.get(COPYRIGHT_URL, {'bible_id': 'ENGESV'})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()['error_code'] == 'provider_error'
    assert 'DBT Error' not in response.json()['error']


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_get_copyright_rate_limited(mock_get_client, client):
    """An upstream 429 maps to HTTP 429 + rate_limited."""
    mock_get_client.return_value.get_copyright.side_effect = (
        FakeProviderError(429)
    )

    response = client.get(COPYRIGHT_URL, {'bible_id': 'ENGESV'})

    assert (
        response.status_code
        == status.HTTP_429_TOO_MANY_REQUESTS
    )
    assert response.json()['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_provider_404_returns_not_found(mock_get_client, client):
    """An upstream 404 maps to HTTP 404 + not_found."""
    mock_get_client.return_value.get_copyright.side_effect = (
        FakeProviderError(404)
    )

    response = client.get(COPYRIGHT_URL, {'bible_id': 'ENGESV'})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()['error_code'] == 'not_found'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_get_copyright_error_does_not_leak_credentials(
    mock_get_client, client
):
    """Exception text can carry ``?key=`` credentials — it must
    never reach the response body."""
    mock_get_client.return_value.get_copyright.side_effect = (
        ConnectionError(
            'Max retries exceeded with url: '
            '/copyright?key=SECRET'
        )
    )

    response = client.get(COPYRIGHT_URL, {'bible_id': 'ENGESV'})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()['error_code'] == 'provider_error'
    assert 'SECRET' not in response.json()['error']


# --- API.Bible branch (views.py:483-501) --------------------------------


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_branch_returns_copyright(
    mock_get_client, client
):
    """API.Bible fileset → copyright built from ``get_bible``."""
    mock_get_client.return_value.get_bible.return_value = {
        'copyrightStatement': 'Copyright Biblica',
    }

    response = client.get(
        COPYRIGHT_URL, {'bible_id': 'ENGNIV_API'}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['data'] == [{
        'id': 'ENGNIV_API',
        'type': 'text_plain',
        'size': 'C',
        'copyright': 'Copyright Biblica',
        'copyright_date': '',
        'copyright_description': 'Copyright Biblica',
    }]
    mock_get_client.return_value.get_bible \
        .assert_called_once_with(APINIV_BIBLE_ID)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_branch_missing_statement_defaults(
    mock_get_client, client
):
    """A missing ``copyrightStatement`` yields '' fields."""
    mock_get_client.return_value.get_bible.return_value = {}

    response = client.get(
        COPYRIGHT_URL, {'bible_id': 'ENGNIV_API'}
    )

    assert response.status_code == status.HTTP_200_OK
    entry = response.json()['data'][0]
    assert entry['copyright'] == ''
    assert entry['copyright_date'] == ''
    assert entry['copyright_description'] == ''


# --- Non-DBT registry routing (plan: bible/copyright #1) ------
#
# ESV/SWORD ids — and their registry abbrs — used to fall
# through to the DBT call and 404 upstream. They now answer
# from registry ``license`` metadata via the provider facade.


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_esv_id_answers_from_registry_license(
    mock_get_client, client
):
    """``ENGESV_API`` returns the ESV registry license — DBT
    is never called for non-DBT filesets."""
    response = client.get(
        COPYRIGHT_URL, {'bible_id': 'ENGESV_API'}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['data'] == [{
        'id': 'ENGESV_API',
        'type': 'text_plain',
        'size': 'C',
        'copyright': 'Copyright Crossway. See api.esv.org.',
        'copyright_date': '',
        'copyright_description': (
            'Copyright Crossway. See api.esv.org.'
        ),
    }]
    mock_get_client.assert_not_called()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_sword_id_and_abbr_answer_from_registry_license(
    mock_get_client, client
):
    """``LVSGLU8`` and its abbr ``GLU8`` return the SWORD
    registry license without touching DBT."""
    expected = [{
        'type': 'text_plain',
        'size': 'C',
        'copyright': 'Public Domain',
        'copyright_date': '',
        'copyright_description': 'Public Domain',
    }]

    for bible_id in ('LVSGLU8', 'GLU8', 'LVSGLU8C1DA'):
        response = client.get(
            COPYRIGHT_URL, {'bible_id': bible_id}
        )
        assert response.status_code == status.HTTP_200_OK
        entry = response.json()['data'][0]
        assert entry['id'] == bible_id
        assert {
            k: v for k, v in entry.items() if k != 'id'
        } == expected[0]

    mock_get_client.assert_not_called()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_apibible_client')
def test_apibible_abbr_canonicalizes_to_text_bible(
    mock_get_client, client
):
    """The ``NIV`` abbr canonicalizes through
    ``canonical_apibible_fileset_id`` and hits the same live
    copyright fetch as ``ENGNIV_API``."""
    mock_get_client.return_value.get_bible.return_value = {
        'copyrightStatement': 'Copyright Biblica',
    }

    response = client.get(COPYRIGHT_URL, {'bible_id': 'NIV'})

    assert response.status_code == status.HTTP_200_OK
    entry = response.json()['data'][0]
    assert entry['id'] == 'NIV'
    assert entry['copyright'] == 'Copyright Biblica'
    mock_get_client.return_value.get_bible \
        .assert_called_once_with(APINIV_BIBLE_ID)
