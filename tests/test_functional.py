"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg2
import pytest

from scrape_pages import (
    get_profiles,
    init_db,
    is_article_scraped,
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


def insert_profile(conn, url: str, config: dict) -> int:
    """Helper: insert a profile with a url and config JSON and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profile (url, config) VALUES (%s, %s) RETURNING id",
            (url, json.dumps(config)),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


DEFAULT_URL = "https://blog.example.com"


def make_config(**kwargs) -> dict:
    """Return a minimal valid blog-scraper config (no url), with optional overrides."""
    cfg = {
        "articles_selector": "a.post",
        "content_selector": "article",
    }
    cfg.update(kwargs)
    return cfg


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


class TestGetProfilesFunctional:
    def test_returns_inserted_profiles(self, db_conn):
        insert_profile(db_conn, DEFAULT_URL, make_config())
        profiles = get_profiles(db_conn)
        assert len(profiles) == 1
        assert profiles[0][1] == DEFAULT_URL

    def test_returns_empty_when_no_profiles(self, db_conn):
        profiles = get_profiles(db_conn)
        assert profiles == []

    def test_multiple_profiles_ordered_by_id(self, db_conn):
        insert_profile(db_conn, "https://blog.example.com/a", make_config())
        insert_profile(db_conn, "https://blog.example.com/b", make_config())
        profiles = get_profiles(db_conn)
        assert len(profiles) == 2
        assert profiles[0][0] < profiles[1][0]


# ---------------------------------------------------------------------------
# connector_scrap
# ---------------------------------------------------------------------------


class TestSaveEntryFunctional:
    def test_row_is_persisted(self, db_conn):
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
        executed_at = datetime.now(tz=timezone.utc)
        params = {"url": "https://blog.example.com/post-1", **make_config()}
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
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
        params = {"url": "https://blog.example.com/post-1", **make_config()}
        save_entry(db_conn, profile_id, "content", params, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT params FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row[0]["url"] == "https://blog.example.com/post-1"

    def test_multiple_entries_per_profile(self, db_conn):
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
        executed_at = datetime.now(tz=timezone.utc)
        save_entry(db_conn, profile_id, "first", {"url": "https://blog.example.com/post-1"}, executed_at)
        save_entry(db_conn, profile_id, "second", {"url": "https://blog.example.com/post-2"}, executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
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
# is_article_scraped
# ---------------------------------------------------------------------------


class TestIsArticleScrapedFunctional:
    def test_returns_true_for_existing_article(self, db_conn):
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
        url = "https://blog.example.com/post-1"
        save_entry(db_conn, profile_id, "content", {"url": url}, datetime.now(tz=timezone.utc))

        assert is_article_scraped(db_conn, profile_id, url) is True

    def test_returns_false_for_unknown_article(self, db_conn):
        profile_id = insert_profile(db_conn, DEFAULT_URL, make_config())
        assert is_article_scraped(db_conn, profile_id, "https://blog.example.com/post-never-seen") is False

    def test_does_not_cross_profiles(self, db_conn):
        profile_a = insert_profile(db_conn, "https://blog-a.example.com", make_config())
        profile_b = insert_profile(db_conn, "https://blog-b.example.com", make_config())
        url = "https://shared.example.com/post-1"
        save_entry(db_conn, profile_a, "content", {"url": url}, datetime.now(tz=timezone.utc))

        assert is_article_scraped(db_conn, profile_a, url) is True
        assert is_article_scraped(db_conn, profile_b, url) is False


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_process_profile_stores_one_article(self, mock_links, mock_scrape, db_conn):
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_scrape.return_value = "Article content"
        config = make_config()
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)

        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, params->>'url', success FROM connector_scrap WHERE provider_id = %s",
                (profile_id,),
            )
            row = cur.fetchone()
        assert row[0] == "Article content"
        assert row[1] == "https://blog.example.com/post-1"
        assert row[2] is True

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_process_profile_stops_at_duplicate(self, mock_links, mock_scrape, db_conn):
        """If the first article on the listing page is already in DB, nothing new is scraped."""
        url = "https://blog.example.com/post-1"
        config = make_config()
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)
        # Pre-insert the article so it's already "known"
        save_entry(db_conn, profile_id, "old content", {"url": url}, datetime.now(tz=timezone.utc))

        mock_links.return_value = [url, "https://blog.example.com/post-2"]

        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        # scrape_page should never be called — stopped at duplicate
        mock_scrape.assert_not_called()

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 1  # Only the pre-inserted one

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_process_profile_respects_max_scraps(self, mock_links, mock_scrape, db_conn):
        mock_links.return_value = [f"https://blog.example.com/post-{i}" for i in range(10)]
        mock_scrape.return_value = "Content"
        config = make_config(max_scraps=3)
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)

        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 3

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_second_run_stops_immediately_when_all_known(self, mock_links, mock_scrape, db_conn):
        """On a second run, if the newest article is already in DB, no new entries are created."""
        url = "https://blog.example.com/post-1"
        config = make_config()
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)

        mock_links.return_value = [url]
        mock_scrape.return_value = "Content"

        # First run — article is new
        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        # Second run — same listing, article already in DB
        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 1  # Only one entry, not two

    @patch("scrape_pages.get_article_links")
    def test_process_profile_logs_error_on_listing_page_failure(self, mock_links, db_conn):
        mock_links.side_effect = Exception("connection timeout")
        config = make_config()
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)

        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert "connection timeout" in row[0]

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_process_profile_logs_error_when_content_selector_not_found(self, mock_links, mock_scrape, db_conn):
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_scrape.return_value = None  # Selector found nothing
        config = make_config()
        profile_id = insert_profile(db_conn, DEFAULT_URL, config)

        process_profile(db_conn, profile_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE profile_id = %s", (profile_id,))
            row = cur.fetchone()
        assert row is not None

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE provider_id = %s", (profile_id,))
            count = cur.fetchone()[0]
        assert count == 0
