"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg2
import pytest

from scrape_pages import (
    MAX_SCRAPS_PER_RUN,
    clean_old_scraps,
    get_repositories,
    has_any_scrap,
    init_db,
    is_article_scraped,
    process_repository,
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
        cur.execute("TRUNCATE connector_scrap, log, repository RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


def insert_repository(conn, url: str, config: dict) -> int:
    """Helper: insert a repository and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repository (url, config) VALUES (%s, %s) RETURNING id",
            (url, json.dumps(config)),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


DEFAULT_URL = "https://blog.example.com"


def make_config(**kwargs) -> dict:
    """Return a minimal valid blog-scraper config, with optional overrides."""
    cfg = {
        "articles_selector": "a.post",
        "content_selector": "article",
    }
    cfg.update(kwargs)
    return cfg


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


class TestGetRepositoriesFunctional:
    def test_returns_inserted_repositories(self, db_conn):
        insert_repository(db_conn, DEFAULT_URL, make_config())
        repositories = get_repositories(db_conn)
        assert len(repositories) == 1
        assert repositories[0][1] == DEFAULT_URL

    def test_returns_empty_when_no_repositories(self, db_conn):
        repositories = get_repositories(db_conn)
        assert repositories == []

    def test_multiple_repositories_ordered_by_id(self, db_conn):
        insert_repository(db_conn, "https://blog.example.com/a", make_config())
        insert_repository(db_conn, "https://blog.example.com/b", make_config())
        repositories = get_repositories(db_conn)
        assert len(repositories) == 2
        assert repositories[0][0] < repositories[1][0]


# ---------------------------------------------------------------------------
# connector_scrap
# ---------------------------------------------------------------------------


class TestSaveEntryFunctional:
    def test_row_is_persisted(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        executed_at = datetime.now(tz=timezone.utc)
        params = {"url": "https://blog.example.com/post-1", **make_config()}
        save_entry(db_conn, repository_id, "Hello world", params, executed_at)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, success FROM connector_scrap WHERE repository_id = %s",
                (repository_id,),
            )
            row = cur.fetchone()
        assert row[0] == "Hello world"
        assert row[1] is True

    def test_params_stored_as_jsonb(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        params = {"url": "https://blog.example.com/post-1", **make_config()}
        save_entry(db_conn, repository_id, "content", params, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT params FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0]["url"] == "https://blog.example.com/post-1"

    def test_multiple_entries_per_repository(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        executed_at = datetime.now(tz=timezone.utc)
        save_entry(db_conn, repository_id, "first", {"url": "https://blog.example.com/post-1"}, executed_at)
        save_entry(db_conn, repository_id, "second", {"url": "https://blog.example.com/post-2"}, executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        executed_at = datetime.now(tz=timezone.utc)
        save_error(db_conn, repository_id, "No element found.", executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT error, repository_id FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0] == "No element found."
        assert row[1] == repository_id

    def test_error_without_repository(self, db_conn):
        save_error(db_conn, None, "network error", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id IS NULL")
            row = cur.fetchone()
        assert row[0] == "network error"


# ---------------------------------------------------------------------------
# has_any_scrap
# ---------------------------------------------------------------------------


class TestHasAnyScrapFunctional:
    def test_returns_false_when_no_articles(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        assert has_any_scrap(db_conn, repository_id) is False

    def test_returns_true_after_first_entry(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        save_entry(
            db_conn, repository_id, "content", {"url": "https://blog.example.com/post-1"}, datetime.now(tz=timezone.utc)
        )
        assert has_any_scrap(db_conn, repository_id) is True

    def test_does_not_cross_repositories(self, db_conn):
        repo_a = insert_repository(db_conn, "https://blog-a.example.com", make_config())
        repo_b = insert_repository(db_conn, "https://blog-b.example.com", make_config())
        save_entry(
            db_conn, repo_a, "content", {"url": "https://blog-a.example.com/post-1"}, datetime.now(tz=timezone.utc)
        )

        assert has_any_scrap(db_conn, repo_a) is True
        assert has_any_scrap(db_conn, repo_b) is False


# ---------------------------------------------------------------------------
# is_article_scraped
# ---------------------------------------------------------------------------


class TestIsArticleScrapedFunctional:
    def test_returns_true_for_existing_article(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        url = "https://blog.example.com/post-1"
        save_entry(db_conn, repository_id, "content", {"url": url}, datetime.now(tz=timezone.utc))

        assert is_article_scraped(db_conn, repository_id, url) is True

    def test_returns_false_for_unknown_article(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        assert is_article_scraped(db_conn, repository_id, "https://blog.example.com/never-seen") is False

    def test_does_not_cross_repositories(self, db_conn):
        repo_a = insert_repository(db_conn, "https://blog-a.example.com", make_config())
        repo_b = insert_repository(db_conn, "https://blog-b.example.com", make_config())
        url = "https://shared.example.com/post-1"
        save_entry(db_conn, repo_a, "content", {"url": url}, datetime.now(tz=timezone.utc))

        assert is_article_scraped(db_conn, repo_a, url) is True
        assert is_article_scraped(db_conn, repo_b, url) is False


# ---------------------------------------------------------------------------
# clean_old_scraps
# ---------------------------------------------------------------------------


class TestCleanOldScrapsFunctional:
    def test_deletes_entries_older_than_15_days(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=16)
        save_entry(db_conn, repository_id, "old article", {"url": "https://blog.example.com/old-1"}, old_date)
        save_entry(db_conn, repository_id, "old article 2", {"url": "https://blog.example.com/old-2"}, old_date)

        deleted = clean_old_scraps(db_conn)

        assert deleted == 2
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 0

    def test_keeps_recent_entries(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=10)
        save_entry(db_conn, repository_id, "recent", {"url": "https://blog.example.com/recent"}, recent_date)

        deleted = clean_old_scraps(db_conn)

        assert deleted == 0
        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1

    def test_deletes_old_keeps_recent(self, db_conn):
        repository_id = insert_repository(db_conn, DEFAULT_URL, make_config())
        old_date = datetime.now(tz=timezone.utc) - timedelta(days=16)
        recent_date = datetime.now(tz=timezone.utc) - timedelta(days=5)
        save_entry(db_conn, repository_id, "old", {"url": "https://blog.example.com/old"}, old_date)
        save_entry(db_conn, repository_id, "recent", {"url": "https://blog.example.com/recent"}, recent_date)

        deleted = clean_old_scraps(db_conn)

        assert deleted == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT content FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row[0] == "recent"

    def test_returns_zero_when_nothing_to_delete(self, db_conn):
        deleted = clean_old_scraps(db_conn)
        assert deleted == 0


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_first_run_saves_only_latest_article(self, mock_links, mock_scrape, db_conn):
        """When no articles exist, only the first (newest) article is saved."""
        mock_links.return_value = [
            "https://blog.example.com/post-3",
            "https://blog.example.com/post-2",
            "https://blog.example.com/post-1",
        ]
        mock_scrape.return_value = "Article content"
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)

        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, params->>'url', success FROM connector_scrap WHERE repository_id = %s",
                (repository_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Article content"
        assert rows[0][1] == "https://blog.example.com/post-3"
        assert rows[0][2] is True

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_subsequent_run_saves_new_articles_up_to_limit(self, mock_links, mock_scrape, db_conn):
        """When articles exist, new ones are saved up to MAX_SCRAPS_PER_RUN."""
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)
        # Pre-insert one existing article
        existing_url = "https://blog.example.com/post-0"
        save_entry(db_conn, repository_id, "existing", {"url": existing_url}, datetime.now(tz=timezone.utc))

        # Mock more new articles than MAX_SCRAPS_PER_RUN, followed by the existing one
        new_urls = [f"https://blog.example.com/post-{i}" for i in range(MAX_SCRAPS_PER_RUN + 5, 0, -1)]
        mock_links.return_value = new_urls + [existing_url]
        mock_scrape.return_value = "Content"

        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1 + MAX_SCRAPS_PER_RUN  # 1 pre-existing + MAX_SCRAPS_PER_RUN new

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_stops_at_duplicate_when_articles_exist(self, mock_links, mock_scrape, db_conn):
        """If the first article on the listing page is already in DB, nothing new is scraped."""
        url = "https://blog.example.com/post-1"
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)
        save_entry(db_conn, repository_id, "old content", {"url": url}, datetime.now(tz=timezone.utc))

        mock_links.return_value = [url, "https://blog.example.com/post-2"]

        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        mock_scrape.assert_not_called()

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1  # Only the pre-inserted one

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_second_run_stops_immediately_when_latest_known(self, mock_links, mock_scrape, db_conn):
        """On a second run, if the newest article is already in DB, no new entries are created."""
        url = "https://blog.example.com/post-1"
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)

        mock_links.return_value = [url]
        mock_scrape.return_value = "Content"

        # First run — article is new, no existing scraps
        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        # Second run — same listing, article already in DB
        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 1  # Only one entry, not two

    @patch("scrape_pages.get_article_links")
    def test_logs_error_on_listing_page_failure(self, mock_links, db_conn):
        mock_links.side_effect = Exception("connection timeout")
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)

        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert "connection timeout" in row[0]

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.get_article_links")
    def test_logs_error_when_content_selector_not_found(self, mock_links, mock_scrape, db_conn):
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_scrape.return_value = None  # Selector found nothing
        config = make_config()
        repository_id = insert_repository(db_conn, DEFAULT_URL, config)

        process_repository(db_conn, repository_id, DEFAULT_URL, config, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repository_id,))
            row = cur.fetchone()
        assert row is not None

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_scrap WHERE repository_id = %s", (repository_id,))
            count = cur.fetchone()[0]
        assert count == 0
