import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.views import LanguageListView


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


DBT_LANGUAGES_RESPONSE = {
    'data': [
        {
            'id': 6414,
            'glotto_id': 'russ1263',
            'iso': 'rus',
            'name': 'Russian',
            'autonym': 'Русский',
            'bibles': 6,
            'filesets': 18,
            'country_population': 137494000,
        },
        {
            'id': 6334,
            'glotto_id': 'bury1244',
            'iso': 'bua',
            'name': 'Buryat',
            'autonym': 'Буряад',
            'bibles': 1,
            'filesets': 2,
            'country_population': 520000,
        },
    ],
    'meta': {
        'pagination': {
            'total': 2,
            'count': 2,
            'per_page': 25,
            'current_page': 1,
            'total_pages': 1,
        }
    },
}


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_param_mapping(mock_get_client, factory):
    """``search``/``code``/``page``/``limit`` map onto DBT's
    ``language_name``/``language_code``/``page``/``limit``."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_languages.return_value = (
        DBT_LANGUAGES_RESPONSE
    )

    request = factory.get('/fake-url/', {
        'search': 'russ',
        'code': 'rus',
        'page': '2',
        'limit': '50',
    })
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    mock_dbt_instance.get_languages.assert_called_once_with(
        language_name='russ',
        language_code='rus',
        page=2,
        limit=50,
    )


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_no_params(mock_get_client, factory):
    """With no query params every DBT filter stays unset."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_languages.return_value = (
        DBT_LANGUAGES_RESPONSE
    )

    request = factory.get('/fake-url/')
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    mock_dbt_instance.get_languages.assert_called_once_with(
        language_name=None,
        language_code=None,
        page=None,
        limit=None,
    )


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_result_shape_and_pagination(
    mock_get_client, factory
):
    """Results expose only the contracted fields (``bibles``
    passthrough included); ``meta.pagination``'s documented
    fields are forwarded."""
    mock_dbt_instance = mock_get_client.return_value
    mock_dbt_instance.get_languages.return_value = (
        DBT_LANGUAGES_RESPONSE
    )

    request = factory.get('/fake-url/', {'search': 'russ'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    results = response.data['results']
    assert len(results) == 2
    assert results[0] == {
        'iso': 'rus',
        'name': 'Russian',
        'autonym': 'Русский',
        'bibles': 6,
        'filesets': 18,
    }
    assert results[0]['bibles'] > 0
    assert results[1]['iso'] == 'bua'
    assert response.data['meta']['pagination'] == (
        DBT_LANGUAGES_RESPONSE['meta']['pagination']
    )


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_dbt_exception(mock_get_client, factory):
    """A DBT failure surfaces provider error fields, not str(exc)."""
    mock_get_client.return_value.get_languages.side_effect = (
        Exception("DBT Error")
    )

    request = factory.get('/fake-url/', {'search': 'russ'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'DBT Error' not in response.data['error']


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_rate_limited(mock_get_client, factory):
    """An upstream 429 maps to HTTP 429 + rate_limited."""
    mock_get_client.return_value.get_languages.side_effect = (
        FakeProviderError(429)
    )

    request = factory.get('/fake-url/', {'search': 'russ'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.data['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_error_does_not_leak_credentials(
    mock_get_client, factory
):
    """Network errors stringify with the request URL — including
    DBT's ``?key=`` credential — so the raw exception text must
    never reach the response body."""
    mock_get_client.return_value.get_languages.side_effect = (
        ConnectionError(
            'Max retries exceeded with url: '
            '/languages?key=SECRET'
        )
    )

    request = factory.get('/fake-url/', {'search': 'russ'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'SECRET' not in response.data['error']


@pytest.mark.django_db
def test_get_languages_invalid_page(factory):
    """A non-integer ``page`` is a client error (400)."""
    request = factory.get('/fake-url/', {'page': 'abc'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'error' in response.data


@pytest.mark.django_db
def test_get_languages_invalid_limit(factory):
    """A non-integer ``limit`` is a client error (400)."""
    request = factory.get('/fake-url/', {'limit': 'abc'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'error' in response.data


@pytest.mark.django_db
@pytest.mark.parametrize(
    'params',
    [
        {'page': '0'},
        {'page': '-1'},
        {'limit': '0'},
        {'limit': '-5'},
        {'page': '-1', 'limit': '0'},
    ],
)
@patch('bible.views.get_default_dbt_client')
def test_get_languages_non_positive_pagination(
    mock_get_client, factory, params
):
    """Non-positive ``page``/``limit`` are client errors (400)
    and must never reach DBT."""
    request = factory.get('/fake-url/', params)
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'error' in response.data
    mock_get_client.return_value.get_languages.assert_not_called()


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_missing_pagination(
    mock_get_client, factory
):
    """Without ``meta.pagination`` upstream, ``meta`` normalises
    to an empty object rather than ``pagination: null``."""
    mock_get_client.return_value.get_languages.return_value = {
        'data': DBT_LANGUAGES_RESPONSE['data'],
    }

    request = factory.get('/fake-url/', {'search': 'russ'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert response.data['meta'] == {}


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_get_languages_pagination_whitelisted(
    mock_get_client, factory
):
    """Only the documented pagination fields are forwarded;
    DBT-internal extras (e.g. ``links``) are stripped."""
    mock_get_client.return_value.get_languages.return_value = {
        'data': [],
        'meta': {
            'pagination': {
                'total': 10,
                'count': 0,
                'per_page': 25,
                'current_page': 2,
                'total_pages': 1,
                'links': {'next': 'https://dbt.example?key=x'},
            },
            'links': {'first': 'https://dbt.example?key=x'},
        },
    }

    request = factory.get('/fake-url/', {'page': '2'})
    response = LanguageListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert response.data['meta'] == {
        'pagination': {
            'total': 10,
            'count': 0,
            'per_page': 25,
            'current_page': 2,
            'total_pages': 1,
        }
    }
