"""Unit tests — no external dependencies (DB, network)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from scrape_pages import (
    cleanup_old_entries,
    get_profiles,
    init_db,
    save_entry,
    save_error,
    scrape_page,
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def make_conn_mock():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestInitDb:
    def test_executes_ddl_and_commits(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()


class TestGetProfiles:
    def test_returns_list_of_tuples(self):
        conn, cursor = make_conn_mock()
        cursor.fetchall.return_value = [
            (1, {"page": "https://example.com", "path": "main"}),
            (2, {"page": "https://example.com/about", "path": "article"}),
        ]
        result = get_profiles(conn)
        assert len(result) == 2
        assert result[0][0] == 1
        assert result[0][1]["page"] == "https://example.com"

    def test_returns_empty_list_when_no_profiles(self):
        conn, cursor = make_conn_mock()
        cursor.fetchall.return_value = []
        result = get_profiles(conn)
        assert result == []

    def test_queries_profile_table(self):
        conn, cursor = make_conn_mock()
        cursor.fetchall.return_value = []
        get_profiles(conn)
        sql = cursor.execute.call_args[0][0]
        assert "profile" in sql


class TestSaveEntry:
    def test_inserts_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        params = {"page": "https://example.com", "path": "main"}
        save_entry(conn, 1, "Hello world", params, executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_correct_params_passed(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        params = {"page": "https://example.com", "path": "main"}
        save_entry(conn, 3, "content", params, executed_at)
        call_params = cursor.execute.call_args[0][1]
        assert call_params[0] == 3  # provider_id
        assert call_params[1] == "content"  # content
        assert call_params[3] == executed_at  # executed_at

    def test_params_serialized_as_json(self):
        conn, cursor = make_conn_mock()
        params = {"page": "https://example.com", "path": "main"}
        save_entry(conn, 1, "content", params, datetime.now(tz=timezone.utc))
        call_params = cursor.execute.call_args[0][1]
        stored = json.loads(call_params[2])
        assert stored["page"] == "https://example.com"

    def test_success_flag_in_sql(self):
        conn, cursor = make_conn_mock()
        save_entry(conn, 1, "content", {}, datetime.now(tz=timezone.utc))
        sql = cursor.execute.call_args[0][0]
        assert "TRUE" in sql


class TestSaveError:
    def test_inserts_error_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        save_error(conn, 5, "something went wrong", executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        params = cursor.execute.call_args[0][1]
        assert params == (5, "something went wrong", executed_at)

    def test_accepts_none_profile_id(self):
        conn, cursor = make_conn_mock()
        save_error(conn, None, "error", datetime.now(tz=timezone.utc))
        params = cursor.execute.call_args[0][1]
        assert params[0] is None


class TestCleanupOldEntries:
    def test_executes_delete_and_commits(self):
        conn, cursor = make_conn_mock()
        cleanup_old_entries(conn, 1)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "DELETE FROM connector_scrap" in sql

    def test_passes_correct_params(self):
        conn, cursor = make_conn_mock()
        cleanup_old_entries(conn, 7)
        params = cursor.execute.call_args[0][1]
        assert params[0] == 7  # provider_id for DELETE
        assert params[1] == 7  # provider_id for subquery
        assert params[2] == 3  # MAX_ENTRIES_PER_PROFILE


# ---------------------------------------------------------------------------
# scrape_page
# ---------------------------------------------------------------------------


class TestScrapePage:
    @patch("scrape_pages.requests.get")
    def test_returns_text_of_matched_element(self, mock_get):
        mock_get.return_value.text = "<html><body><main><p>Hello world</p></main></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "main")
        assert result == "Hello world"

    @patch("scrape_pages.requests.get")
    def test_returns_none_when_no_match(self, mock_get):
        mock_get.return_value.text = "<html><body><div>content</div></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = scrape_page("https://example.com", "article")
        assert result is None

    @patch("scrape_pages.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("404 Not Found")
        try:
            scrape_page("https://example.com", "main")
            assert False, "Should have raised"
        except Exception as e:
            assert "404" in str(e)
