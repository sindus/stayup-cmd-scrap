# stayup-cmd-scrap

Monitors web pages by scraping CSS-selected content and storing results in PostgreSQL.

On each run the script fetches every profile from the database, scrapes the element
at the configured CSS path, and persists the result. The three most recent entries
per profile are kept.

## How it works

Pages to monitor are stored directly in the `profile` table as JSON configs:

```sql
INSERT INTO profile (config)
VALUES ('{"page": "https://example.com", "path": "main article"}');
```

Each run scrapes all profiles and inserts a new row into `connector_scrap`.
Errors are logged to the `log` table.

## Database schema

```
profile
  id          SERIAL PK
  config      JSONB UNIQUE   -- {"page": "...", "path": "..."}
  created_at  TIMESTAMPTZ

connector_scrap
  id          SERIAL PK
  provider_id → profile.id
  content     TEXT           -- scraped text content
  params      JSONB          -- snapshot of the profile config used
  executed_at TIMESTAMPTZ
  success     BOOLEAN

log
  id          SERIAL PK
  profile_id  → profile.id
  error       TEXT
  executed_at TIMESTAMPTZ
```

## Setup

### With Docker (recommended)

```bash
cp .env.example .env
# Fill in DB_NAME, DB_USER, DB_PASSWORD

docker compose up db -d

# Add a page to monitor
docker compose exec db psql -U <DB_USER> -d <DB_NAME> -c \
  "INSERT INTO profile (config) VALUES ('{\"page\": \"https://example.com\", \"path\": \"main\"}');"

# Run the scraper
docker compose run --rm scrape_pages
```

### Without Docker

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:password@host:5432/dbname
python scrape_pages.py
```

## GitHub Actions

### CI (`ci.yml`)

Runs on every push and pull request to `main`:
- Lint with **ruff** and **black**
- Run unit + functional tests against a temporary PostgreSQL service

### Daily cron (`daily.yml`)

Runs every day at 08:00 UTC (also triggerable manually from GitHub Actions).

Required secret:
- `DATABASE_URL` — connection string to your production database

To configure: **Settings → Secrets and variables → Actions → New repository secret**

## Development

```bash
pip install -r requirements-dev.txt

# Lint
ruff check .
black --check .

# Tests (unit only, no DB required)
pytest tests/test_unit.py -v

# All tests (requires PostgreSQL)
DB_HOST=localhost DB_NAME=stayup DB_USER=stayup DB_PASSWORD=stayup pytest tests/ -v
```

### Pre-commit hook

```bash
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```
