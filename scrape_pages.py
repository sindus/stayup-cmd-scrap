#!/usr/bin/env python3
"""
Stayup — scrapes blog articles defined in the profile table and stores results in PostgreSQL.

For each profile, the script fetches the listing page, extracts article URLs using the
configured CSS selector, and scrapes each article until one is found that already exists
in the database or the per-run limit is reached.

Expected profile config shape:
  {
    "page":               "https://blog.example.com",        # listing page URL
    "articles_selector":  "h2.post-title a",                 # CSS selector for article links
    "content_selector":   "article.post-content",            # CSS selector for article body (optional, default: "body")
    "max_scraps":         20                                  # per-run limit (optional, default: MAX_SCRAPS_PER_RUN)
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
CREATE TABLE IF NOT EXISTS profile (
    id          SERIAL PRIMARY KEY,
    config      JSONB NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_scrap (
    id          SERIAL PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES profile(id),
    content     TEXT NOT NULL,
    params      JSONB NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL,
    success     BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          SERIAL PRIMARY KEY,
    profile_id  INTEGER,
    error       TEXT NOT NULL,
    executed_at TIMESTAMPTZ NOT NULL
);
"""

# Default maximum number of articles scraped per profile per run.
MAX_SCRAPS_PER_RUN = 50


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


def get_profiles(conn: psycopg2.extensions.connection) -> list[tuple[int, dict]]:
    """Return all profiles as a list of (id, config) tuples."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, config FROM profile ORDER BY id")
        return cur.fetchall()


def save_entry(
    conn: psycopg2.extensions.connection,
    profile_id: int,
    content: str,
    params: dict,
    executed_at: datetime,
) -> None:
    """Persist a scrape result to the database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO connector_scrap (provider_id, content, params, executed_at, success)
            VALUES (%s, %s, %s, %s, TRUE)
            """,
            (profile_id, content, json.dumps(params, ensure_ascii=False), executed_at),
        )
    conn.commit()


def save_error(
    conn: psycopg2.extensions.connection,
    profile_id: int | None,
    error: str,
    executed_at: datetime,
) -> None:
    """Persist a scrape error to the log table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO log (profile_id, error, executed_at)
            VALUES (%s, %s, %s)
            """,
            (profile_id, error, executed_at),
        )
    conn.commit()


def is_article_scraped(conn: psycopg2.extensions.connection, profile_id: int, article_url: str) -> bool:
    """Return True if this article URL was already scraped for the given profile."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM connector_scrap WHERE provider_id = %s AND params->>'url' = %s LIMIT 1",
            (profile_id, article_url),
        )
        return cur.fetchone() is not None


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


def process_profile(
    conn: psycopg2.extensions.connection,
    profile_id: int,
    config: dict,
    executed_at: datetime,
) -> None:
    """Scrape blog articles for one profile and persist new results.

    For each article URL found on the listing page:
      - Stop if the article URL already exists in the database (duplicate boundary reached).
      - Stop if the per-run scrape limit is reached.
    Any exception during listing-page fetch is caught, logged, and printed to stderr.
    Errors on individual articles are logged but do not stop the run.
    """
    try:
        page = config["page"]
        articles_selector = config["articles_selector"]
        content_selector = config.get("content_selector", "body")
        max_scraps = int(config.get("max_scraps", MAX_SCRAPS_PER_RUN))

        article_urls = get_article_links(page, articles_selector)

        scraped_count = 0
        for url in article_urls:
            if scraped_count >= max_scraps:
                break

            if is_article_scraped(conn, profile_id, url):
                break  # Already in DB — no need to go further back

            try:
                content = scrape_page(url, content_selector)
                if content is None:
                    save_error(
                        conn,
                        profile_id,
                        f"No element found at selector '{content_selector}' on {url}",
                        executed_at,
                    )
                    continue

                save_entry(conn, profile_id, content, {"url": url, **config}, executed_at)
                scraped_count += 1

            except Exception as e:
                save_error(conn, profile_id, f"Error scraping {url}: {e}", executed_at)
                print(f"[{url}] Error: {e}", file=sys.stderr)

    except Exception as e:
        save_error(conn, profile_id, str(e), executed_at)
        print(f"[{config.get('page', '?')}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    conn = get_db_conn()
    try:
        init_db(conn)

        profiles = get_profiles(conn)
        if not profiles:
            print("No profiles tracked. Insert rows into the profile table to add pages.")
            return

        executed_at = datetime.now(tz=timezone.utc)

        for profile_id, config in profiles:
            process_profile(conn, profile_id, config, executed_at)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
