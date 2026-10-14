import pytest
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
