# Bloomberg Scraper

A Python script that collects news links from Google News RSS and enriches them
with publication dates and descriptions from publisher pages. Bloomberg articles
are handled in a separate final pass that can also extract author information.

The scraper runs three stages in order:

1. **RSS collection** — searches Google News for each configured source within a
   date range and saves article titles, links, publishers, and RSS publication
   dates.
2. **Article enrichment** — decodes Google News links and looks for publication
   dates and descriptions in publisher pages. It uses HTTP requests first and
   can use a local browser for pages that need it.
3. **Bloomberg enrichment** — checks Bloomberg articles that are still missing a
   description and uses Playwright with an installed Google Chrome browser to
   extract additional metadata.

## Requirements

- Python 3.9 or newer
- An internet connection
- Google Chrome, Brave, or Microsoft Edge for the optional browser fallback in
  stage 2 (Chrome is required for stage 3 when Bloomberg articles need
  enrichment)

## Setup

From the project directory, create and activate a virtual environment in
PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Install the dependencies:

```powershell
pip install -r requirements.txt
```

If PowerShell blocks virtual-environment activation, activate it from Command
Prompt instead:

```bat
.venv\Scripts\activate.bat
```

### Environment & Database Configuration

Copy the sample environment file to `.env` and fill in your credentials:

```powershell
cp .env.example .env
```

Edit `.env` to configure your PostgreSQL connection:
```env
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_secure_password_here
DB_NAME=news_db
```

> **Security Note:** The `.env` file is excluded in `.gitignore` so your actual credentials are never committed.

## Run

Run the scraper:
```powershell
python feed.py
```

Import articles into PostgreSQL:
```powershell
python import_to_postgres.py
```

View and verify imported articles:
```powershell
python check_db.py
```

The script runs all three stages sequentially. It reads `source_counts.json` for
the source list when that file is present; otherwise it uses a small built-in
default list. The source file can be a JSON list of strings:

```json
["Reuters", "BBC", "NPR"]
```

It also accepts a list of objects with a `source` field, as in the included
`source_counts.json`:

```json
[
  {"source": "Reuters", "count": 938},
  {"source": "BBC", "count": 453}
]
```

Only the `source` value is used; the `count` is informational. The script
processes at most `MAX_SOURCES` entries from the list.

## Configuration

Settings are constants near the top of [`feed.py`](./feed.py). Edit them before
running the script:

- `AFTER_DATE` and `BEFORE_DATE` — inclusive-style query boundaries passed to
  Google News search (formatted as `YYYY-MM-DD`).
- `ARTICLES_PER_SOURCE` and `MAX_SOURCES` — collection limits.
- `RSS_WORKERS` — number of concurrent RSS source fetches.
- `SKIP_BROWSER` — skip the stage 2 browser fallback when set to `True`.
- `RETRY_FAILED` — when `True`, clears the previous failed-article list and
  retries articles that still need enrichment.
- `PHASE1_WORKERS`, `PHASE2_BROWSERS`, and `TABS_PER_BROWSER` — enrichment
  concurrency settings.

Stage 2 looks for Chrome, Brave, or Edge on Windows. To specify a browser
executable explicitly, set `FEED_BROWSER_EXECUTABLE` to its full path before
running the script:

```powershell
$env:FEED_BROWSER_EXECUTABLE = "C:\Path\To\chrome.exe"
python feed.py
```

Stage 3 launches the installed Google Chrome browser in a visible window. It
does not use Playwright's bundled Chromium as a substitute for the Chrome
channel configured in the script.

## Files

The script reads and writes these files in the current working directory:

| File | Purpose |
| --- | --- |
| `source_counts.json` | Source queries for RSS collection |
| `google_news_articles.json` | Raw RSS results and collection progress |
| `google_news_articles_enriched.json` | Merged results with enriched metadata |
| `url_cache.json` | Cache of decoded Google News URLs |
| `failed_articles.json` | Articles that could not be enriched |

The JSON results contain a top-level `metadata` object and an `articles` object
grouped by source. Enriched articles may include fields such as `published_at`,
`original_published_at`, `description`, `author`, and the publisher URL.

RSS collection resumes from sources already present in its output file. The
enrichment stage also reuses existing enriched data, URL cache entries, and
progress saved during processing. These files are working data; keep copies if
you need to preserve a particular run.

## Notes

- The script depends on Google News RSS and publisher pages remaining available;
  results, dates, and descriptions are not guaranteed.
- Publishers may block automated requests, require subscriptions, or present
  challenges. The browser fallback does not guarantee access to those pages.
- Use the scraper responsibly and follow the applicable terms of service,
  robots policies, and copyright rules for Google News and each publisher.

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).