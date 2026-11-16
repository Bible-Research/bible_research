import pytest
import requests
from unittest.mock import Mock
from bible.services.dbt.client import DBTClient


@pytest.fixture
def dbt_client():
    """Fixture to provide a DBTClient with a mocked HTTP session."""
    client = DBTClient()
    client.session = Mock()
    return client


def test_ssl_verification_enabled(dbt_client):
    """
    Regression test: TLS verification must stay enabled on the
    generated OpenAPI client (see issue #54).
    """
    assert dbt_client.api_client.configuration.verify_ssl is True


def test_session_requests_verify_tls(dbt_client):
    """
    Raw requests calls must not disable TLS verification.
    """
    mock_response = Mock()
    mock_response.json.return_value = {"data": []}
    mock_response.raise_for_status = Mock()
    dbt_client.session.get.return_value = mock_response

    dbt_client.get_timestamps("fileset", "book", "chapter")
    dbt_client.get_copyright("ENGESV")
    dbt_client.search("ENGESV", "query")

    assert dbt_client.session.get.call_count == 3
    for call in dbt_client.session.get.call_args_list:
        _, call_kwargs = call
        assert call_kwargs.get('verify') is not False


def test_get_timestamps_calls_api(dbt_client):
    """
    Test that get_timestamps calls session.get with the
    correct URL and parameters.
    """
    mock_response = Mock()
    mock_response.json.return_value = {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 5.5},
        ]
    }
    mock_response.raise_for_status = Mock()
    dbt_client.session.get.return_value = mock_response

    fileset_id = "ENGESVN2DA"
    book = "JHN"
    chapter = "1"

    result = dbt_client.get_timestamps(fileset_id, book, chapter)

    dbt_client.session.get.assert_called_once()
    call_args, call_kwargs = dbt_client.session.get.call_args
    assert 'v=4' in call_args[0] or call_kwargs.get(
        'params', {}
    ).get('v') == 4
    assert result == {
        "data": [
            {"verse_start": 1, "timestamp": 0.0},
            {"verse_start": 2, "timestamp": 5.5},
        ]
    }


def test_get_timestamps_injects_version(dbt_client):
    """
    Test that get_timestamps passes v=4 as a query parameter.
    """
    mock_response = Mock()
    mock_response.json.return_value = {"data": []}
    mock_response.raise_for_status = Mock()
    dbt_client.session.get.return_value = mock_response

    dbt_client.get_timestamps("fileset", "book", "chapter")

    call_args, call_kwargs = dbt_client.session.get.call_args
    params = call_kwargs.get('params', {})
    assert params.get('v') == 4


def test_get_copyright_calls_api(dbt_client):
    """
    Test that get_copyright calls session.get with
    the correct URL and parameters.
    """
    mock_response = Mock()
    mock_response.json.return_value = [
        {"id": "ENGESV", "copyright": {}}
    ]
    mock_response.raise_for_status = Mock()
    dbt_client.session.get.return_value = mock_response

    result = dbt_client.get_copyright("ENGESV")

    dbt_client.session.get.assert_called_once()
    call_args, call_kwargs = dbt_client.session.get.call_args
    assert 'ENGESV' in call_args[0]
    assert call_kwargs.get('params', {}).get('v') == 4
    assert result == [
        {"id": "ENGESV", "copyright": {}}
    ]


def test_get_books_uses_raw_session(dbt_client):
    """
    ``get_books`` calls ``GET /bibles/{id}/book`` through the
    shared requests session and returns the raw JSON dict —
    the generated client's ``v4_bible_books`` deserializer is
    broken (phantom ``Id``/``Name`` model types), so this must
    never route back through ``bibles_api``.
    """
    mock_response = Mock()
    mock_response.json.return_value = {
        "data": [{"book_id": "GEN", "name": "Genesis"}]
    }
    mock_response.raise_for_status = Mock()
    dbt_client.session.get.return_value = mock_response

    result = dbt_client.get_books("LAVNLI", verify_content=True)

    call_args, call_kwargs = dbt_client.session.get.call_args
    assert call_args[0].endswith("/bibles/LAVNLI/book")
    params = call_kwargs.get('params', {})
    assert params.get('v') == 4
    assert params.get('key') == dbt_client.api_key
    assert params.get('verify_content') is True
    assert result == {
        "data": [{"book_id": "GEN", "name": "Genesis"}]
    }


def test_get_books_error_hides_url_credential(dbt_client):
    """
    ``requests`` exceptions stringify with the full request
    URL — including the ``?key=`` credential — and propagate
    into view logs and Sentry. ``get_books`` must re-raise a
    credential-free copy that still carries ``response`` for
    ``upstream_status_code`` classification.
    """
    resp = Mock()
    resp.status_code = 404
    original = requests.exceptions.HTTPError(
        "404 Client Error: Not Found for url: "
        "https://b4.dbt.io/api/bibles/X/book?key=SECRET"
    )
    original.response = resp
    dbt_client.session.get.side_effect = original

    with pytest.raises(requests.exceptions.HTTPError) as exc:
        dbt_client.get_books("XXXXXX")

    assert 'key=' not in str(exc.value)
    assert 'SECRET' not in str(exc.value)
    assert exc.value.response is resp
