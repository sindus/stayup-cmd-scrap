#!/usr/bin/env python3
"""
Stayup — scrapes blog articles defined in the repository table and stores results in PostgreSQL.

For each repository, the script fetches the listing page and extracts article URLs:
  - If no articles exist yet for this repository: saves only the latest article.
  - Otherwise: saves new articles (newest first) until a known article is found,
    up to MAX_SCRAPS_PER_RUN articles per run.

A cleanup step removes connector_scrap entries older than 15 days.

Repository table columns:
  url     TEXT   — listing page URL to scrape
  config  JSONB  — scraping options:
    {
      "articles_selector":  "h2.post-title a",       # CSS selector for article links
      "content_selector":   "article.post-content",  # CSS selector for article body (optional, default: "body")
    }
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import psycopg2
import requests
from bs4 import BeautifulSoup

DDL = """
CREATE TABLE IF NOT EXISTS repository (
    id          SERIAL PRIMARY KEY,
    url         TEXT NOT NULL UNIQUE,
    config      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_scrap (
    id          SERIAL PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repository(id),
    content     TEXT NOT NULL,
    params      JSONB NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL,
    success     BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          SERIAL PRIMARY KEY,
    repository_id  INTEGER,
    error       TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
);
"""

# Maximum number of articles scraped per repository per run (when articles already exist in DB).
MAX_SCRAPS_PER_RUN = 5


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db_conn() -> psycopg2.extensions.connection:
    """Return a psycopg2 connection.

    Reads DATABASE_URL first; falls back to individual DB_* environment
    variables (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def init_db(conn: psycopg2.extensions.connection) -> None:
    """Create tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def get_repositories(conn: psycopg2.extensions.connection) -> list[tuple[int, str, dict]]:
    """Return all repositories as a list of (id, url, config) tuples."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, config FROM repository ORDER BY id")
        return cur.fetchall()


def save_entry(
    conn: psycopg2.extensions.connection,
    repository_id: int,
    content: str,
    params: dict,
    executed_at: datetime,
) -> None:
    """Persist a scrape result to the database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO connector_scrap (repository_id, content, params, executed_at, success)
            VALUES (%s, %s, %s, %s, TRUE)
            """,
            (repository_id, content, json.dumps(params, ensure_ascii=False), executed_at),
        )
    conn.commit()


def save_error(
    conn: psycopg2.extensions.connection,
    repository_id: int | None,
    error: str,
    executed_at: datetime,
) -> None:
    """Persist a scrape error to the log table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO log (repository_id, error, executed_at)
            VALUES (%s, %s, %s)
            """,
            (repository_id, error, executed_at),
        )
    conn.commit()


def has_any_scrap(conn: psycopg2.extensions.connection, repository_id: int) -> bool:
    """Return True if at least one scraped article exists for the given repository."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM connector_scrap WHERE repository_id = %s LIMIT 1",
            (repository_id,),
        )
        return cur.fetchone() is not None


def is_article_scraped(conn: psycopg2.extensions.connection, repository_id: int, article_url: str) -> bool:
    """Return True if this article URL was already scraped for the given repository."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM connector_scrap WHERE repository_id = %s AND params->>'url' = %s LIMIT 1",
            (repository_id, article_url),
        )
        return cur.fetchone() is not None


def clean_old_scraps(conn: psycopg2.extensions.connection) -> int:
    """Delete connector_scrap rows older than 15 days. Returns the number of deleted rows."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM connector_scrap WHERE executed_at < NOW() - INTERVAL '15 days'")
        deleted = cur.rowcount
    conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


def get_article_links(page_url: str, articles_selector: str) -> list[str]:
    """Fetch a listing page and return absolute URLs of all elements matching articles_selector.

    Elements must have an href attribute (typically <a> tags).
    Relative hrefs are resolved against page_url.
    """
    resp = requests.get(page_url, timeout=30, headers={"User-Agent": "stayup-scrap/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for element in soup.select(articles_selector):
        href = element.get("href")
        if href:
            links.append(urljoin(page_url, href))
    return links


def scrape_page(page_url: str, css_path: str) -> str | None:
    """Fetch a page and return the text content of the element matching css_path.

    Returns None if no element matches the selector.
    """
    resp = requests.get(page_url, timeout=30, headers={"User-Agent": "stayup-scrap/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    element = soup.select_one(css_path)
    if element is None:
        return None
    return element.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_repository(
    conn: psycopg2.extensions.connection,
    repository_id: int,
    page: str,
    config: dict,
    executed_at: datetime,
) -> None:
    """Scrape blog articles for one repository and persist new results.

    - If no articles exist yet for this repository: saves only the latest article.
    - Otherwise: iterates articles newest-first, saves new ones, stops at the first
      already-known article or after max_scraps articles.
    Any exception during listing-page fetch is caught, logged, and printed to stderr.
    Errors on individual articles are logged but do not stop the run.
    """
    try:
        articles_selector = config["articles_selector"]
        content_selector = config.get("content_selector", "body")
        max_scraps = MAX_SCRAPS_PER_RUN

        article_urls = get_article_links(page, articles_selector)
        if not article_urls:
            return

        if not has_any_scrap(conn, repository_id):
            # First time: save only the latest article
            url = article_urls[0]
            try:
                content = scrape_page(url, content_selector)
                if content is None:
                    save_error(
                        conn,
                        repository_id,
                        f"No element found at selector '{content_selector}' on {url}",
                        executed_at,
                    )
                else:
                    save_entry(conn, repository_id, content, {"url": url, **config}, executed_at)
            except Exception as e:
                save_error(conn, repository_id, f"Error scraping {url}: {e}", executed_at)
                print(f"[{url}] Error: {e}", file=sys.stderr)
            return

        # Articles exist: save new ones until we hit a known one
        scraped_count = 0
        for url in article_urls:
            if scraped_count >= max_scraps:
                break

            if is_article_scraped(conn, repository_id, url):
                break

            try:
                content = scrape_page(url, content_selector)
                if content is None:
                    save_error(
                        conn,
                        repository_id,
                        f"No element found at selector '{content_selector}' on {url}",
                        executed_at,
                    )
                    continue

                save_entry(conn, repository_id, content, {"url": url, **config}, executed_at)
                scraped_count += 1

            except Exception as e:
                save_error(conn, repository_id, f"Error scraping {url}: {e}", executed_at)
                print(f"[{url}] Error: {e}", file=sys.stderr)

    except Exception as e:
        save_error(conn, repository_id, str(e), executed_at)
        print(f"[{page}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    conn = get_db_conn()
    try:
        init_db(conn)
        clean_old_scraps(conn)

        repositories = get_repositories(conn)
        if not repositories:
            print("No repositories tracked. Insert rows into the repository table to add pages.")
            return

        executed_at = datetime.now(tz=timezone.utc)

        for repository_id, url, config in repositories:
            process_repository(conn, repository_id, url, config, executed_at)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
