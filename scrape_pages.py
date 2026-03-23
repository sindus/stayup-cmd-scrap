#!/usr/bin/env python3
"""
Stayup — scrapes web pages defined in the profile table and stores results in PostgreSQL.

On each run the script fetches all profiles from the database, scrapes the page
at the CSS selector path stored in the profile config, and persists the result.
The three most recent entries per profile are kept.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

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

# Maximum number of scrape entries kept per profile.
MAX_ENTRIES_PER_PROFILE = 3


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


def cleanup_old_entries(conn: psycopg2.extensions.connection, profile_id: int) -> None:
    """Delete scrape entries beyond the MAX_ENTRIES_PER_PROFILE most recent ones."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM connector_scrap
            WHERE provider_id = %s
              AND id NOT IN (
                SELECT id FROM connector_scrap
                WHERE provider_id = %s
                ORDER BY executed_at DESC
                LIMIT %s
              )
            """,
            (profile_id, profile_id, MAX_ENTRIES_PER_PROFILE),
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


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


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
    """Scrape one profile and persist the result.

    After saving, old entries beyond MAX_ENTRIES_PER_PROFILE are pruned.
    Any exception is caught, logged to the `log` table, and printed to stderr.
    """
    try:
        content = scrape_page(config["page"], config["path"])
        if content is None:
            raise RuntimeError(f"No element found at path: {config['path']}")

        save_entry(conn, profile_id, content, config, executed_at)
        cleanup_old_entries(conn, profile_id)

    except Exception as e:
        save_error(conn, profile_id, str(e), executed_at)
        print(f"[{config['page']}] Error: {e}", file=sys.stderr)


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
