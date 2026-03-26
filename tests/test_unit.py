"""Unit tests — no external dependencies (DB, network)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from scrape_pages import (
    get_article_links,
    get_profiles,
    init_db,
    is_article_scraped,
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
            (1, {"page": "https://example.com", "articles_selector": "a.post"}),
            (2, {"page": "https://example.com/about", "articles_selector": "article a"}),
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
        params = {"url": "https://example.com/post-1", "page": "https://example.com"}
        save_entry(conn, 1, "Hello world", params, executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_correct_params_passed(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        params = {"url": "https://example.com/post-1", "page": "https://example.com"}
        save_entry(conn, 3, "content", params, executed_at)
        call_params = cursor.execute.call_args[0][1]
        assert call_params[0] == 3  # provider_id
        assert call_params[1] == "content"  # content
        assert call_params[3] == executed_at  # executed_at

    def test_params_serialized_as_json(self):
        conn, cursor = make_conn_mock()
        params = {"url": "https://example.com/post-1", "page": "https://example.com"}
        save_entry(conn, 1, "content", params, datetime.now(tz=timezone.utc))
        call_params = cursor.execute.call_args[0][1]
        stored = json.loads(call_params[2])
        assert stored["url"] == "https://example.com/post-1"

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


# ---------------------------------------------------------------------------
# is_article_scraped
# ---------------------------------------------------------------------------


class TestIsArticleScraped:
    def test_returns_true_when_row_found(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = (1,)
        assert is_article_scraped(conn, 1, "https://example.com/post-1") is True

    def test_returns_false_when_no_row(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = None
        assert is_article_scraped(conn, 1, "https://example.com/post-1") is False

    def test_query_uses_provider_id_and_url(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = None
        is_article_scraped(conn, 7, "https://example.com/post-1")
        sql = cursor.execute.call_args[0][0]
        assert "provider_id" in sql
        assert "params->>'url'" in sql
        params = cursor.execute.call_args[0][1]
        assert params[0] == 7
        assert params[1] == "https://example.com/post-1"


# ---------------------------------------------------------------------------
# get_article_links
# ---------------------------------------------------------------------------


class TestGetArticleLinks:
    @patch("scrape_pages.requests.get")
    def test_returns_absolute_urls(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>"
            '<a class="post" href="https://example.com/post-1">Post 1</a>'
            '<a class="post" href="https://example.com/post-2">Post 2</a>'
            "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/post-1", "https://example.com/post-2"]

    @patch("scrape_pages.requests.get")
    def test_resolves_relative_hrefs(self, mock_get):
        mock_get.return_value.text = "<html><body>" '<a class="post" href="/blog/post-1">Post 1</a>' "</body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/blog/post-1"]

    @patch("scrape_pages.requests.get")
    def test_skips_elements_without_href(self, mock_get):
        mock_get.return_value.text = (
            "<html><body>" '<a class="post">No href</a>' '<a class="post" href="/post-1">With href</a>' "</body></html>"
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == ["https://example.com/post-1"]

    @patch("scrape_pages.requests.get")
    def test_returns_empty_list_when_no_match(self, mock_get):
        mock_get.return_value.text = "<html><body><p>No links here</p></body></html>"
        mock_get.return_value.raise_for_status = MagicMock()
        result = get_article_links("https://example.com", "a.post")
        assert result == []

    @patch("scrape_pages.requests.get")
    def test_raises_on_http_error(self, mock_get):
        mock_get.return_value.raise_for_status.side_effect = Exception("404 Not Found")
        try:
            get_article_links("https://example.com", "a.post")
            assert False, "Should have raised"
        except Exception as e:
            assert "404" in str(e)


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


# ---------------------------------------------------------------------------
# process_profile
# ---------------------------------------------------------------------------


class TestProcessProfile:
    """Unit tests for process_profile — all external calls are mocked."""

    def _make_config(self, max_scraps=None):
        cfg = {
            "page": "https://blog.example.com",
            "articles_selector": "a.post",
            "content_selector": "article",
        }
        if max_scraps is not None:
            cfg["max_scraps"] = max_scraps
        return cfg

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.is_article_scraped")
    @patch("scrape_pages.get_article_links")
    def test_saves_new_article(self, mock_links, mock_is_scraped, mock_scrape):
        from scrape_pages import process_profile

        conn, _ = make_conn_mock()
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_is_scraped.return_value = False
        mock_scrape.return_value = "Article content"

        process_profile(conn, 1, self._make_config(), datetime.now(tz=timezone.utc))

        mock_scrape.assert_called_once_with("https://blog.example.com/post-1", "article")
        conn.commit.assert_called()

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.is_article_scraped")
    @patch("scrape_pages.get_article_links")
    def test_stops_when_article_already_scraped(self, mock_links, mock_is_scraped, mock_scrape):
        from scrape_pages import process_profile

        conn, _ = make_conn_mock()
        mock_links.return_value = ["https://blog.example.com/post-1", "https://blog.example.com/post-2"]
        mock_is_scraped.return_value = True  # Both are already in DB

        process_profile(conn, 1, self._make_config(), datetime.now(tz=timezone.utc))

        mock_scrape.assert_not_called()

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.is_article_scraped")
    @patch("scrape_pages.get_article_links")
    def test_stops_at_max_scraps(self, mock_links, mock_is_scraped, mock_scrape):
        from scrape_pages import process_profile

        conn, _ = make_conn_mock()
        mock_links.return_value = [f"https://blog.example.com/post-{i}" for i in range(10)]
        mock_is_scraped.return_value = False
        mock_scrape.return_value = "Content"

        process_profile(conn, 1, self._make_config(max_scraps=3), datetime.now(tz=timezone.utc))

        assert mock_scrape.call_count == 3

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.is_article_scraped")
    @patch("scrape_pages.get_article_links")
    def test_logs_error_when_content_selector_not_found(self, mock_links, mock_is_scraped, mock_scrape):
        from scrape_pages import process_profile

        conn, cursor = make_conn_mock()
        mock_links.return_value = ["https://blog.example.com/post-1"]
        mock_is_scraped.return_value = False
        mock_scrape.return_value = None  # Selector found nothing

        process_profile(conn, 1, self._make_config(), datetime.now(tz=timezone.utc))

        # An error row should have been inserted (save_error calls cursor.execute)
        assert cursor.execute.call_count >= 1

    @patch("scrape_pages.get_article_links")
    def test_logs_error_on_listing_page_failure(self, mock_links):
        from scrape_pages import process_profile

        conn, cursor = make_conn_mock()
        mock_links.side_effect = Exception("connection timeout")

        process_profile(conn, 1, self._make_config(), datetime.now(tz=timezone.utc))

        # save_error must have been called
        assert cursor.execute.call_count >= 1

    @patch("scrape_pages.scrape_page")
    @patch("scrape_pages.is_article_scraped")
    @patch("scrape_pages.get_article_links")
    def test_continues_after_per_article_error(self, mock_links, mock_is_scraped, mock_scrape):
        from scrape_pages import process_profile

        conn, _ = make_conn_mock()
        mock_links.return_value = [
            "https://blog.example.com/post-1",
            "https://blog.example.com/post-2",
        ]
        mock_is_scraped.return_value = False
        mock_scrape.side_effect = [Exception("timeout"), "Content of post 2"]

        process_profile(conn, 1, self._make_config(), datetime.now(tz=timezone.utc))

        # Second article was still scraped despite first failing
        assert mock_scrape.call_count == 2
