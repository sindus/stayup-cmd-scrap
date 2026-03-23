"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg2
import pytest

from scrape_pages import (
    cleanup_old_entries,
    get_profiles,
    init_db,
    process_profile,
    save_entry,
    save_error,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_conn():
    try:
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ.get("DB_NAME", "stayup"),
            user=os.environ.get("DB_USER", "stayup"),
            password=os.environ.get("DB_PASSWORD", "stayup"),
        )
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL unavailable: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Create tables once for the whole test session."""
    conn = make_conn()
    init_db(conn)
    conn.close()


@pytest.fixture
def db_conn():
    """Fresh connection per test to guarantee isolation."""
    conn = make_conn()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE connector_scrap, log, profile RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


def insert_profile(conn, config: dict) -> int:
    """Helper: insert a profile with a config JSON and return its id."""
    import json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profile (config) VALUES (%s) RETURNING id",
            (json.dumps(config),),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class TestGetProfilesFunctional:
    def test_returns_inserted_profiles(self, db_conn):
        config = {"page": "https://example.com", "path": "main"}
        insert_profile(db_conn, config)
        profiles = get_profiles(db_conn)
        assert len(profiles) == 1
        assert profiles[0][1]["page"] == "https://example.com"

    def test_returns_empty_when_no_profiles(self, db_conn):
        profiles = get_profiles(db_conn)
        assert profiles == []

    def test_multiple_profiles_ordered_by_id(self, db_conn):
        insert_profile(db_conn, {"page": "https://example.com/a", "path": "main"})
        insert_profile(db_conn, {"page": "https://example.com/b", "path": "article"})
        profiles = get_profiles(db_conn)
        assert len(profiles) == 2
        assert profiles[0][0] < profiles[1][0]


# ---------------------------------------------------------------------------
# connector_scrap
# ---------------------------------------------------------------------------


class TestSaveEntryFunctional:
    def test_row_is_persisted(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        executed_at = datetime.now(tz=timezone.utc)
        params = {"page": "https://example.com", "path": "main"}
        save_entry(db_conn, profile_id, "Hello world", params, executed_at)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, success FROM connector_scrap WHERE provider_id = %s",
                (profile_id,),
            )
            row = cur.fetchone()
        assert row[0] == "Hello world"
        assert row[1] is True

    def test_params_stored_as_jsonb(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        params = {"page": "https://example.com", "path": "main"}
        save_entry(db_conn, profile_id, "content", params, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT params FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0]["page"] == "https://example.com"
        assert row[0]["path"] == "main"

    def test_multiple_entries_per_profile(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        executed_at = datetime.now(tz=timezone.utc)
        save_entry(db_conn, profile_id, "first", {}, executed_at)
        save_entry(db_conn, profile_id, "second", {}, executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanupOldEntriesFunctional:
    def test_keeps_only_last_3(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        executed_at = datetime.now(tz=timezone.utc)
        for i in range(5):
            save_entry(db_conn, profile_id, f"content {i}", {}, executed_at)

        cleanup_old_entries(db_conn, profile_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 3

    def test_does_nothing_when_less_than_3(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        save_entry(db_conn, profile_id, "content", {}, datetime.now(tz=timezone.utc))

        cleanup_old_entries(db_conn, profile_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        profile_id = insert_profile(db_conn, {"page": "https://example.com", "path": "main"})
        executed_at = datetime.now(tz=timezone.utc)
        save_error(db_conn, profile_id, "No element found.", executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT error, profile_id FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0] == "No element found."
        assert row[1] == profile_id

    def test_error_without_profile(self, db_conn):
        save_error(db_conn, None, "network error", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id IS NULL")
            row = cur.fetchone()
        assert row[0] == "network error"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @patch("scrape_pages.scrape_page")
    def test_process_profile_stores_result(self, mock_scrape, db_conn):
        """Each run always stores a new entry."""
        mock_scrape.return_value = "Welcome to React"
        config = {"page": "https://fr.react.dev/learn", "path": "article"}
        profile_id = insert_profile(db_conn, config)
        process_profile(db_conn, profile_id, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, success FROM connector_scrap WHERE provider_id = %s",
                (profile_id,),
            )
            row = cur.fetchone()
        assert row[0] == "Welcome to React"
        assert row[1] is True

    @patch("scrape_pages.scrape_page")
    def test_process_profile_stores_on_every_run(self, mock_scrape, db_conn):
        """Two runs produce two entries regardless of content."""
        mock_scrape.return_value = "Same content"
        config = {"page": "https://example.com", "path": "main"}
        profile_id = insert_profile(db_conn, config)
        process_profile(db_conn, profile_id, config, datetime.now(tz=timezone.utc))
        process_profile(db_conn, profile_id, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 2

    @patch("scrape_pages.scrape_page")
    def test_process_profile_logs_error_on_failure(self, mock_scrape, db_conn):
        """Network error — logged to the log table."""
        mock_scrape.side_effect = Exception("connection timeout")
        config = {"page": "https://example.com", "path": "main"}
        profile_id = insert_profile(db_conn, config)
        process_profile(db_conn, profile_id, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert "connection timeout" in row[0]

    @patch("scrape_pages.scrape_page")
    def test_process_profile_logs_error_when_no_element(self, mock_scrape, db_conn):
        """Element not found — logged to the log table."""
        mock_scrape.return_value = None
        config = {"page": "https://example.com", "path": "#nonexistent"}
        profile_id = insert_profile(db_conn, config)
        process_profile(db_conn, profile_id, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row is not None
