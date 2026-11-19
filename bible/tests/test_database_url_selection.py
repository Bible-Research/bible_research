"""Tests for `settings._database_url_for_environment` — the
per-environment DB pin. Deployed envs must never fall back to
prod implicitly."""
import pytest
from django.core.exceptions import ImproperlyConfigured

from bible_research import settings as s

_ENV_KEYS = (
    'VERCEL_ENV', 'VERCEL',
    'DATABASE_URL', 'DATABASE_STAGING_URL',
    'DATABASE_LOCAL_URL', 'DATABASE_READ_URL',
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(s, 'IS_GCP_ENVIRONMENT', False)


def test_vercel_preview_uses_staging_url(monkeypatch):
    monkeypatch.setenv('VERCEL_ENV', 'preview')
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    monkeypatch.setenv(
        'DATABASE_STAGING_URL', 'postgres://h/stagingdb')
    assert s._database_url_for_environment() == (
        'postgres://h/stagingdb')


def test_vercel_preview_missing_staging_raises(monkeypatch):
    monkeypatch.setenv('VERCEL_ENV', 'preview')
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    with pytest.raises(ImproperlyConfigured):
        s._database_url_for_environment()


def test_vercel_production_uses_database_url(monkeypatch):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('VERCEL_ENV', 'production')
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    monkeypatch.setenv(
        'DATABASE_STAGING_URL', 'postgres://h/stagingdb')
    assert s._database_url_for_environment() == (
        'postgres://h/defaultdb')


def test_vercel_production_missing_url_raises(monkeypatch):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('VERCEL_ENV', 'production')
    with pytest.raises(ImproperlyConfigured):
        s._database_url_for_environment()


def test_unknown_vercel_env_raises(monkeypatch):
    """A deployed Vercel context with an unrecognized
    VERCEL_ENV must not fall through to prod."""
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('VERCEL_ENV', 'surprise')
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    with pytest.raises(ImproperlyConfigured):
        s._database_url_for_environment()


def test_vercel_dev_counts_as_local(monkeypatch):
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.setenv('VERCEL_ENV', 'development')
    monkeypatch.setenv(
        'DATABASE_LOCAL_URL', 'sqlite:///db.sqlite3')
    assert s._database_url_for_environment() == (
        'sqlite:///db.sqlite3')


def test_gcp_uses_database_url(monkeypatch):
    monkeypatch.setattr(s, 'IS_GCP_ENVIRONMENT', True)
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    monkeypatch.setenv(
        'DATABASE_STAGING_URL', 'postgres://h/stagingdb')
    assert s._database_url_for_environment() == (
        'postgres://h/defaultdb')


def test_local_fallback_order(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgres://h/defaultdb')
    monkeypatch.setenv('DATABASE_READ_URL', 'postgres://h/read')
    monkeypatch.setenv('DATABASE_LOCAL_URL', 'sqlite:///l.db')
    assert s._database_url_for_environment() == 'sqlite:///l.db'
    monkeypatch.delenv('DATABASE_LOCAL_URL')
    assert s._database_url_for_environment() == (
        'postgres://h/read')
    monkeypatch.delenv('DATABASE_READ_URL')
    assert s._database_url_for_environment() == (
        'postgres://h/defaultdb')
    monkeypatch.delenv('DATABASE_URL')
    assert s._database_url_for_environment() is None
