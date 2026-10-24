

import time
import feedparser                # Parses RSS/Atom feeds
import json                      # Reading / writing JSON
import os                        # File existence checks
import re                        # Regex for date parsing
import random                    # Random delays for browser simulation
import threading                 # Locks for thread‑safe access
import asyncio                   # Async I/O for nodriver browser
import sys                       # Clean exit
from datetime import datetime
from zoneinfo import ZoneInfo    # Timezone support (Python 3.9+)
from typing import List, Dict, Optional
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import requests                  # HTTP requests (also used in Phase 3 fallback)
from bs4 import BeautifulSoup    # HTML parsing

# -----------------------------------------------------------------------------
#  CONFIGURATION – Phase 1 (RSS)
# -----------------------------------------------------------------------------
AFTER_DATE = "2024-12-01"
BEFORE_DATE = "2026-02-16"
ARTICLES_PER_SOURCE = 1000         # max articles to fetch per source
MAX_SOURCES = 15                  # max number of sources to process
RSS_WORKERS = 70                  # parallel threads for RSS fetching
SOURCES_FILE = "source_counts.json"
RSS_OUTPUT_FILE = "google_news_articles.json"
RSS_SAVE_INTERVAL = 10            # save progress every N sources

# -----------------------------------------------------------------------------
#  CONFIGURATION – Phase 2 (Enrichment)
# -----------------------------------------------------------------------------
INPUT_JSON = RSS_OUTPUT_FILE       # input to enrichment (raw RSS)
OUTPUT_JSON = "google_news_articles_enriched.json"
SKIP_BROWSER = False               # set True to skip the browser fallback
RETRY_FAILED = True                # retry all previously failed articles
FAILED_FILE = "failed_articles.json"
CACHE_FILE = "url_cache.json"

ET = ZoneInfo("America/New_York")  # all dates converted to Eastern Time
UTC = ZoneInfo("UTC")

PHASE1_WORKERS = 8                 # threads for requests phase
PHASE2_BROWSERS = 3                # number of browser instances
TABS_PER_BROWSER = 4               # tabs per browser
REQUEST_TIMEOUT = 12               # seconds before requests time out
ENRICH_SAVE_INTERVAL = 50          # save enriched data every N articles

# -----------------------------------------------------------------------------
#  CONFIGURATION – Phase 3 (Bloomberg Playwright)
# -----------------------------------------------------------------------------
# Use the enriched file as input/output (Bloomberg articles will be added to it)
BLOOMBERG_INPUT_FILE = OUTPUT_JSON

# -----------------------------------------------------------------------------
#  UTILITIES
# -----------------------------------------------------------------------------
def load_json(path, default):
    """Load JSON file if it exists, else return default."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    """Write a Python object to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# -----------------------------------------------------------------------------
#  PHASE 1 – RSS SCRAPER
# -----------------------------------------------------------------------------
def build_google_news_rss_url(query: str, after_date: str, before_date: str,
                              language: str = "en", country: str = "US") -> str:
    """
    Build a Google News RSS URL with date filters.

    Why this exists:
    - Google News exposes its search results through RSS feeds.
    - Those feeds can be filtered by a query and a date range.
    - We use this because it is much faster than crawling the web page manually.

    Example query:
    - "bbc after:2021-12-01 before:2022-02-16"

    The function URL-encodes the search string so Google can read it correctly.
    """
    encoded_query = quote(f"{query} after:{after_date} before:{before_date}")
    return f"https://news.google.com/rss/search?q={encoded_query}&hl={language}-{country}&gl={country}&ceid={country}:{language}"

def fetch_articles_for_source(query: str, after_date: str, before_date: str,
                              limit: int = 100) -> List[Dict[str, str]]:
    """
    Fetch up to `limit` articles for a single query from Google News RSS.
    Returns a list of dicts with keys: title, link, published, source, query.
    """
    feed_url = build_google_news_rss_url(query, after_date, before_date)
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"  Error parsing feed for '{query}': {e}")
        return []

    articles = []
    for entry in feed.entries:
        source = entry.get('source', {}).get('title', query) or query
        published = entry.get('published', '')
        articles.append({
            'title': entry.get('title', ''),
            'link': entry.get('link', ''),
            'published': published,
            'source': source,
            'query': query
        })
        if len(articles) >= limit:
            break
    return articles

def save_rss_data(results: Dict, total_articles: int, sources_processed: int,
                  output_file: str):
    """Save RSS scraping progress with metadata."""
    data = {
        'metadata': {
            'total_articles': total_articles,
            'sources_processed': sources_processed,
            'timestamp': datetime.now().isoformat(),
        },
        'articles': results
    }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  💾 RSS progress saved to {output_file} ({total_articles} articles)")

def scrape_google_news(
        sources: List[str], after_date: str, before_date: str,
        articles_per_source: int = 100, max_sources: int = 100,
        output_file: str = "google_news_articles.json",
        save_interval: int = 10,
        workers: int = 20,
        resume: bool = True
    ) -> Dict[str, List[Dict[str, str]]]:
    """
    Main RSS scraping function.

    What this function is doing:
    - It loops through the sources the user provided.
    - For each source, it calls Google News RSS and collects a batch of article links.
    - It runs those requests in parallel using multiple worker threads, which makes
      the process much faster than doing one source at a time.

    Why resumption matters:
    - If the script stops halfway, we do not want to lose all progress.
    - The function can load the previous JSON file and continue from there.

    Why saving periodically matters:
    - Large scraping jobs can take a long time.
    - Saving every few sources means the script can recover if it crashes.
    """
    # --- Resumption logic ---
    existing_results = {}
    processed_sources = set()
    if resume and os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if 'articles' in old_data:
                    existing_results = old_data['articles']
                    processed_sources = set(existing_results.keys())
                    print(f"✅ Resuming RSS: found {len(existing_results)} sources already in {output_file}")
        except Exception as e:
            print(f"⚠️ Could not load existing data: {e}")

    # Only process sources that haven't been scraped yet
    sources_to_process = [s for s in sources[:max_sources] if s not in processed_sources]
    if not sources_to_process:
        print("✅ All sources already processed. RSS scraping complete.")
        return existing_results

    print(f"📰 Starting RSS scraper (resuming: {len(existing_results)} sources done, {len(sources_to_process)} to go)")
    print(f"📅 Date Range: {after_date} to {before_date}")
    print(f"📚 Remaining sources: {len(sources_to_process)}")
    print("-" * 60)

    results = existing_results.copy()
    total_articles = sum(len(v) for v in results.values())
    sources_processed = len(results)
    lock = threading.Lock()

    def process_source(query):
        """Worker function: fetch articles for one source."""
        articles = fetch_articles_for_source(query, after_date, before_date, articles_per_source)
        return query, articles

    # ----- Manual executor management (avoids hang on Ctrl+C) -----
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(process_source, q): q for q in sources_to_process}
        for future in as_completed(futures):
            query, articles = future.result()
            with lock:
                if articles:
                    results[query] = articles
                    sources_processed += 1
                    total_articles += len(articles)
                    print(f"  ✅ Found {len(articles)} articles for '{query}'")
                else:
                    print(f"  ⚠️ No articles for '{query}'")
                if sources_processed % save_interval == 0:
                    save_rss_data(results, total_articles, sources_processed, output_file)
    except KeyboardInterrupt:
        print("\n⚠️ RSS scraping interrupted by user. Saving progress...")
        save_rss_data(results, total_articles, sources_processed, output_file)
        # Stop immediately – do NOT wait for running threads
        executor.shutdown(wait=False, cancel_futures=True)  # requires Python 3.9+
        print("🛑 Exiting.")
        sys.exit(0)
    else:
        # Normal completion: wait for all threads to finish
        executor.shutdown(wait=True)

    # Final save
    save_rss_data(results, total_articles, sources_processed, output_file)
    print("\n" + "=" * 60)
    print(f"✅ RSS Scraping Complete!")
    print(f"📊 Sources processed: {sources_processed}")
    print(f"📄 Total articles: {total_articles}")
    print(f"💾 Data saved to: {output_file}")
    print("=" * 60)
    return results

# -----------------------------------------------------------------------------
#  PHASE 2 – ENRICHMENT (decode URLs, scrape dates & descriptions)
# -----------------------------------------------------------------------------
# Sources to skip entirely (paywalled or unreliable) – Bloomberg is skipped here,
# it will be handled later by Phase 3.
SKIP_SOURCES = {
    "wsj.com", "wsj", "wall street journal",
    "bloomberg.com", "bloomberg",
    "msn.com", "msn", "microsoft",
    "bizjournals.com", "bizjournals",
    "kiplinger.com", "kiplinger",
    "livemint.com", "mint",
    "business-standard.com",
    "stocktwits.com", "stocktwits",
    "mlq.ai",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# Global state for enrichment (safe because only one enrichment run at a time)
enrich_file_lock = Lock()
URL_CACHE = load_json(CACHE_FILE, {})           # decoded URL cache
FAILED = load_json(FAILED_FILE, [])             # failed articles list
FAILED_SET = {f["google_url"] for f in FAILED}  # quick membership check
ENRICH_DATA = {}                                # merged RSS + enriched data

# -----------------------------------------------------------------------------
#  URL DECODER
# -----------------------------------------------------------------------------
def decode_google_url(url):
    """
    Convert a Google News redirect URL into the real article URL.

    Why this step is necessary:
    - Google News often wraps the original article in a redirect URL.
    - If we store that redirect URL directly, it is less useful for later scraping.
    - We want the real article URL so we can visit the publisher's page directly.

    Strategy:
    1. Try a helper library first, if available.
    2. If that fails, make a real HTTP request and follow the redirects.
    3. If neither works, return None and mark the article as failed.
    """
    if "news.google.com" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(url)
        if isinstance(result, list):
            result = result[0] if result else {}
        if isinstance(result, dict) and result.get("success"):
            decoded = result.get("decoded_url")
            if isinstance(decoded, str) and "google.com" not in decoded:
                return decoded
    except Exception as e:
        print(f"Library error: {e}")
    # Fallback: follow redirects manually
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.url != url and "news.google.com" not in r.url:
            return r.url
    except Exception as e:
        print(f"Redirect error: {e}")
    print("[FAIL] Could not decode, returning None.")
    return None

# -----------------------------------------------------------------------------
#  DATETIME PARSING (Phase 2)
# -----------------------------------------------------------------------------
def to_et_str(dt):
    """Convert a datetime object to Eastern Time string (e.g., '2022-01-15 03:45:00 PM EST')."""
    dt = dt.astimezone(ET)
    abbr = "EDT" if dt.dst() and dt.dst().total_seconds() != 0 else "EST"
    return dt.strftime(f"%Y-%m-%d %I:%M:%S %p {abbr}")

def parse_dt(text):
    """
    Try to parse a raw date/time string into a formatted Eastern Time string.

    Why this is important:
    - News sites publish dates in many different formats.
    - Some use ISO 8601, others use human-readable strings like "January 10, 2022".
    - We need a consistent format so the final dataset is easier to analyze.

    The function tries several common patterns and converts them to Eastern Time.
    This makes the output more readable and more consistent across sources.
    """
    if not text:
        return None
    text = text.strip()
    # Normalise: trim excessive microseconds, fix timezone offset
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)

    # ISO formats
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return to_et_str(dt)
        except Exception:
            pass

    # fromisoformat
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return to_et_str(dt)
    except Exception:
        pass

    # "Month DD, YYYY HH:MM AM/PM"
    m = re.search(r"(\w+ \d{1,2},?\s*\d{4})\s*(?:at\s*)?(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM))", text, re.I)
    if m:
        for fmt in ["%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p",
                    "%B %d %Y %I:%M %p", "%b %d %Y %I:%M %p"]:
            try:
                dt = datetime.strptime(f"{m.group(1).replace(',', '')} {m.group(2).strip()}", fmt)
                return to_et_str(dt.replace(tzinfo=ET))
            except Exception:
                pass

    # "MM/DD/YYYY HH:MM AM/PM"
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}),?\s*(\d{1,2}:\d{2}\s*(?:AM|PM))", text, re.I)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2).strip()}", "%m/%d/%Y %I:%M %p")
            return to_et_str(dt.replace(tzinfo=ET))
        except Exception:
            pass
    return None

# -----------------------------------------------------------------------------
#  HTML EXTRACTION
# -----------------------------------------------------------------------------
CF_SIGNATURES = [
    "just a moment", "checking your browser", "cf-browser-verification",
    "challenge-platform", "enable javascript and cookies to continue",
]

def is_blocked(html):
    """Check if the page is a Cloudflare challenge or similar blocking page."""
    if not html:
        return True
    low = html.lower()[:3000]
    return any(s in low for s in CF_SIGNATURES)

def get_domain(url):
    """Extract domain from URL, stripping 'www.'."""
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""

def should_skip(source, domain=""):
    """Return True if the source or domain is in the skip list."""
    s = source.lower()
    d = domain.lower()
    return any(skip in s or skip in d for skip in SKIP_SOURCES)

def extract_from_jsonld(soup):
    """
    Extract publication date and description from JSON-LD structured data.
    Many news sites embed this in <script type="application/ld+json">.
    """
    result = {"date": None, "description": "", "body": ""}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                nodes = [item] + item.get("@graph", [])
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    if not result["date"]:
                        for key in ("datePublished", "dateCreated", "uploadDate"):
                            if node.get(key):
                                result["date"] = parse_dt(node[key])
                                if result["date"]:
                                    break
                    if not result["description"] and node.get("description"):
                        result["description"] = node["description"][:500]
                    if not result["body"] and node.get("articleBody"):
                        result["body"] = node["articleBody"][:800]
        except Exception:
            continue
        if result["date"] and result["description"]:
            break
    return result

def extract_from_meta(soup):
    """Look for publication date in common <meta> tags."""
    props = [
        "article:published_time", "og:published_time", "datePublished",
        "pubdate", "publish_date", "date", "DC.date.issued", "sailthru.date",
    ]
    for prop in props:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            d = parse_dt(tag["content"])
            if d:
                return d
    return None

def extract_from_time_tags(soup):
    """Look for <time> elements that contain a datetime or text."""
    for tag in soup.find_all("time"):
        raw = tag.get("datetime") or tag.get_text(strip=True)
        if raw:
            d = parse_dt(raw)
            if d:
                return d
    return None

def extract_from_classes(soup):
    """
    Search elements with CSS classes commonly used for dates/timestamps.
    Uses a set of CSS selectors and tries to parse their content.
    """
    selectors = [
        "[class*='publish']", "[class*='posted']", "[class*='date']",
        "[class*='timestamp']", "[class*='byline']",
        "span[data-testid*='date']", "div[data-testid*='date']",
    ]
    for sel in selectors:
        try:
            for tag in soup.select(sel):
                raw = tag.get("datetime") or tag.get("content") or tag.get_text(strip=True)
                if raw and 4 < len(raw) < 100:
                    d = parse_dt(raw)
                    if d:
                        return d
        except Exception:
            continue
    return None

def extract_description_fallback(soup):
    """Fallback description: og:description, meta description, or first <p>s in <article>."""
    tag = soup.find("meta", property="og:description")
    if tag and tag.get("content"):
        return tag["content"][:500]
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return tag["content"][:500]
    article = soup.find("article")
    if article:
        paras = [p.get_text(strip=True) for p in article.find_all("p")[:6]]
        text = " ".join(p for p in paras if p)
        if text:
            return text[:500]
    return ""

def parse_html(html, domain=""):
    """
    Main HTML parser: returns a dict with 'date' and 'description' if successful.
    Combines structured data, meta tags, time tags, and class‑based extraction.
    """
    if not html or is_blocked(html):
        return None
    soup = BeautifulSoup(html, "lxml")

    ld = extract_from_jsonld(soup)
    date = ld["date"]
    description = ld["description"] or ld["body"]

    if not date:
        date = extract_from_meta(soup)
    if not date:
        date = extract_from_time_tags(soup)
    if not date:
        date = extract_from_classes(soup)

    if not description:
        description = extract_description_fallback(soup)

    if not date:
        return None
    return {"date": date, "description": description}

# -----------------------------------------------------------------------------
#  ENRICHMENT UPDATE FUNCTIONS
# -----------------------------------------------------------------------------
def save_enriched_data():
    """Write global ENRICH_DATA to OUTPUT_JSON (thread‑safe)."""
    with enrich_file_lock:
        save_json(OUTPUT_JSON, ENRICH_DATA)

def update_article(google_url, real_url, extracted, article_ref):
    """
    Update the article dictionary with scraped data.
    Replaces 'link' with real URL, adds 'google_link', removes old fields,
    and inserts enriched fields.
    """
    with enrich_file_lock:
        article_ref["google_link"] = google_url
        article_ref["link"] = real_url
        article_ref["original_published_at"] = article_ref.get("published", "")
        article_ref["published_at"] = extracted["date"]
        article_ref.pop("published", None)
        article_ref.pop("query", None)
        article_ref["time_source"] = extracted.get("source", "scraped")
        article_ref["time_tier"] = "scraped"
        if extracted.get("description"):
            article_ref["description"] = extracted["description"]

def mark_failed(google_url, real_url, source, title, error):
    """Record an article that could not be enriched, avoiding duplicates."""
    with enrich_file_lock:
        if google_url not in FAILED_SET:
            FAILED_SET.add(google_url)
            FAILED.append({
                "google_url":  google_url,
                "real_url":    real_url or "",
                "source":      source,
                "title":       title[:120],
                "error":       error,
                "failed_at":   datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
            })
            save_json(FAILED_FILE, FAILED)

# -----------------------------------------------------------------------------
#  ARTICLE COLLECTOR (loads RSS, merges existing enrichment, returns missing)
# -----------------------------------------------------------------------------
def collect_articles():
    """
    Load raw RSS data and any existing enriched data, merge them,
    and return a list of articles that still lack a 'published_at'.

    Why this function exists:
    - The raw RSS file contains the initial article list.
    - The enriched file may already contain some improved metadata from previous runs.
    - This function combines both so we do not lose previous work.

    What it does in practice:
    - It loads the raw RSS JSON and the already-enriched JSON.
    - It matches articles by source and Google News link.
    - If an article was already enriched, its metadata is reused.
    - If not, it is added to the processing queue.
    """
    global ENRICH_DATA

    # 1. Load raw RSS
    rss_data = load_json(INPUT_JSON, {})
    if not rss_data or "articles" not in rss_data:
        print(f"Error: {INPUT_JSON} missing 'articles' key.")
        return []

    # 2. Load existing enriched data (if any)
    enriched_data = load_json(OUTPUT_JSON, {})
    enriched_articles = enriched_data.get('articles', {}) if enriched_data else {}

    # 3. Build a lookup by (source, google_link)
    lookup = {}
    for source, art_list in enriched_articles.items():
        for art in art_list:
            google_link = art.get("google_link")
            if google_link:
                lookup[(source, google_link)] = art

    # 4. Iterate over RSS, merge, and collect missing
    ENRICH_DATA = {"metadata": rss_data.get("metadata", {}), "articles": {}}
    articles_to_process = []

    for source, rss_articles in rss_data.get("articles", {}).items():
        ENRICH_DATA["articles"][source] = []
        for rss_art in rss_articles:
            google_link = rss_art.get("link")  # original Google News link
            key = (source, google_link)
            if key in lookup:
                # Already enriched – copy all fields
                enriched_art = lookup[key]
                for field in ["published_at", "time_source", "time_tier", "description",
                              "original_published_at", "link", "google_link"]:
                    if field in enriched_art:
                        rss_art[field] = enriched_art[field]
                rss_art.pop("published", None)
                rss_art.pop("query", None)
            # Add (merged) article to the output data
            ENRICH_DATA["articles"][source].append(rss_art)
            # If still missing 'published_at', mark for processing
            if "published_at" not in rss_art:
                if google_link and "news.google.com" in google_link:
                    articles_to_process.append({
                        "google_url": google_link,
                        "source":     source,
                        "title":      rss_art.get("title", ""),
                        "article_ref": rss_art,
                    })
    return articles_to_process

# -----------------------------------------------------------------------------
#  ENRICHMENT PHASE 1 – REQUESTS (fast scraping)
# -----------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def phase1_worker(item):
    """
    Worker for Phase 1 enrichment.
    Decodes Google URL, fetches page with requests, tries to extract date/desc.
    Returns (status, item, real_url, extracted_dict).
    """
    google_url = item["google_url"]
    source = item["source"]
    if should_skip(source):
        return "skip", item, None, None

    real_url = decode_google_url(google_url)
    if not real_url:
        return "decode_fail", item, None, None

    domain = get_domain(real_url)
    if should_skip(source, domain):
        return "skip", item, real_url, None

    item["real_url"] = real_url
    item["domain"] = domain

    try:
        r = SESSION.get(real_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or "utf-8"
            extracted = parse_html(r.text, domain)
            if extracted:
                extracted["source"] = f"requests/{extracted.get('source','html')}"
                return "ok", item, real_url, extracted
            return "needs_browser", item, real_url, None
        if r.status_code in (403, 429, 503):
            return "needs_browser", item, real_url, None
    except Exception:
        pass
    return "needs_browser", item, real_url, None

# -----------------------------------------------------------------------------
#  ENRICHMENT PHASE 2 – NODRIVER BROWSER (handles blocked sites)
# -----------------------------------------------------------------------------
async def phase2_browser(items):
    """
    Enrich articles using nodriver (Chromium) for sites that block simple requests.

    Why a browser is used here:
    - Some publishers use anti-bot protections such as Cloudflare or other challenges.
    - A simple HTTP request may receive a blocking page instead of the article content.
    - A real browser can often get past these barriers better than plain requests.

    How this works:
    - The function splits the work across a few browser instances.
    - Each browser loads the article page, waits a bit to look more human, and extracts
      the content from the page HTML.
    - If the site blocks access, the article is marked as failed for later review.
    """
    if not items:
        return

    try:
        import nodriver as uc
    except ImportError:
        print("  [browser] nodriver not installed — skipping")
        for item in items:
            mark_failed(item["google_url"], item.get("real_url"),
                        item["source"], item["title"], "nodriver_missing")
        return

    browser_executable_path = os.environ.get("FEED_BROWSER_EXECUTABLE")
    if browser_executable_path:
        if not os.path.isfile(browser_executable_path):
            raise FileNotFoundError(
                f"FEED_BROWSER_EXECUTABLE does not point to a file: {browser_executable_path}"
            )
    else:
        browser_candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        browser_executable_path = next(
            (path for path in browser_candidates if path and os.path.isfile(path)),
            None,
        )
        if browser_executable_path is None:
            raise FileNotFoundError(
                "No supported browser executable was found. Install Chrome, Brave, or Edge, "
                "or set FEED_BROWSER_EXECUTABLE to the full path of its .exe file."
            )

    # Split items among browser instances
    n_browsers = min(PHASE2_BROWSERS, max(1, (len(items) + TABS_PER_BROWSER - 1) // TABS_PER_BROWSER))
    chunks = [[] for _ in range(n_browsers)]
    for i, item in enumerate(items):
        chunks[i % n_browsers].append(item)

    results = {"ok": 0, "no_date": 0, "blocked": 0, "error": 0}
    counter = {"i": 0}
    total = len(items)
    lock = asyncio.Lock()

    async def open_tab_with_retry(browser, url, max_attempts=3, delay=2):
        for attempt in range(max_attempts):
            try:
                tab = await browser.get(url, new_tab=True)
                return tab
            except (StopIteration, RuntimeError, Exception):
                print("⏳", end=" ", flush=True)
                await asyncio.sleep(delay)
        return None

    async def process_one(browser, item, bid):
        real_url = item.get("real_url") or item["google_url"]
        google_url = item["google_url"]
        source = item["source"]
        title = item["title"][:45]
        domain = item.get("domain") or get_domain(real_url)

        async with lock:
            counter["i"] += 1
            idx = counter["i"]

        print(f"  [B{bid}] [{idx}/{total}] {title[:30]}...", end=" ", flush=True)

        try:
            tab = await open_tab_with_retry(browser, real_url)
            if tab is None:
                raise Exception("Failed to open tab")

            # Mimic human behaviour
            await asyncio.sleep(random.uniform(3, 6))
            await tab.scroll_down(random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.5, 1.5))
            html = await tab.get_content()
            await tab.close()

            if is_blocked(html):
                print("⏳", end=" ", flush=True)
                await asyncio.sleep(10)
                tab2 = await open_tab_with_retry(browser, real_url, max_attempts=2)
                if tab2 is None:
                    print("⚠️ BLOCKED (tab open failed)")
                    async with lock:
                        results["blocked"] += 1
                    mark_failed(google_url, real_url, source, title, "cloudflare_blocked")
                    return
                await asyncio.sleep(4)
                html = await tab2.get_content()
                await tab2.close()
                if is_blocked(html):
                    print("⚠️ BLOCKED")
                    async with lock:
                        results["blocked"] += 1
                    mark_failed(google_url, real_url, source, title, "cloudflare_blocked")
                    return

            extracted = parse_html(html, domain)
            if extracted:
                extracted["source"] = "browser/html"
                update_article(google_url, real_url, extracted, item["article_ref"])
                desc = extracted.get("description", "")[:60]
                print(f"✓ {extracted['date']}  {desc}")
                async with lock:
                    results["ok"] += 1
            else:
                print("✗ no date")
                async with lock:
                    results["no_date"] += 1
                mark_failed(google_url, real_url, source, title, "no_date_in_html")

        except Exception as e:
            print(f"✗ {str(e)[:50]}")
            async with lock:
                results["error"] += 1
            mark_failed(google_url, real_url, source, title, f"browser_error: {str(e)[:80]}")

    async def run_browser(chunk, bid):
        browser = None
        try:
            browser = await uc.start(
                browser_executable_path=browser_executable_path,
                headless=False,
                browser_args=["--window-size=1280,800", "--disable-blink-features=AutomationControlled", "--lang=en-US"],
            )
            await asyncio.sleep(2)

            # Warm‑up
            warmup_ok = False
            for attempt in range(3):
                try:
                    await browser.get("https://www.google.com")
                    warmup_ok = True
                    break
                except Exception as e:
                    print(f"  [B{bid}] Warm-up attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(2)
            if not warmup_ok:
                print(f"  [B{bid}] ❌ Warm-up failed, skipping")
                return

            for i in range(0, len(chunk), TABS_PER_BROWSER):
                batch = chunk[i:i + TABS_PER_BROWSER]
                tasks = [process_one(browser, item, bid) for item in batch]
                await asyncio.gather(*tasks)
                await asyncio.sleep(random.uniform(1, 2))

        finally:
            if browser:
                browser.stop()
                await asyncio.sleep(0.5)

    await asyncio.gather(*[run_browser(chunk, bid + 1) for bid, chunk in enumerate(chunks) if chunk])
    await asyncio.sleep(0.5)
    print(f"\n  [browser] ok={results['ok']}  no_date={results['no_date']}  "
          f"blocked={results['blocked']}  error={results['error']}")

# -----------------------------------------------------------------------------
#  ENRICHMENT ORCHESTRATOR
# -----------------------------------------------------------------------------
def run_enrichment():
    global URL_CACHE, FAILED, FAILED_SET, ENRICH_DATA

    # Load previous state
    FAILED = load_json(FAILED_FILE, [])
    FAILED_SET = {f["google_url"] for f in FAILED}
    URL_CACHE.update(load_json(CACHE_FILE, {}))

    if RETRY_FAILED:
        print("  RETRY_FAILED = True: clearing failed list and reprocessing all articles", flush=True)
        FAILED = []
        FAILED_SET.clear()
        save_json(FAILED_FILE, FAILED)

    print("=" * 80, flush=True)
    print("  GOOGLE NEWS ENRICHMENT", flush=True)
    print(f"  Input  : {INPUT_JSON}", flush=True)
    print(f"  Output : {OUTPUT_JSON}", flush=True)
    print(f"  Cache  : {len(URL_CACHE)} decoded URLs", flush=True)
    print(f"  Browser concurrency: {PHASE2_BROWSERS} browsers × {TABS_PER_BROWSER} tabs = {PHASE2_BROWSERS * TABS_PER_BROWSER} parallel", flush=True)
    print("=" * 80, flush=True)

    all_articles = collect_articles()
    if not all_articles:
        print("  All articles already enriched. Nothing to process.", flush=True)
        return

    from collections import Counter
    by_source = Counter(a["source"] for a in all_articles)
    print(f"\n  {len(all_articles)} articles to process (missing published_at):", flush=True)
    for src, cnt in by_source.most_common(15):
        skip_note = " [SKIP]" if should_skip(src) else ""
        print(f"    {src:<35} {cnt}{skip_note}", flush=True)

    # ----- Phase 1 (requests) -----
    print(f"\n{'─' * 80}", flush=True)
    print(f"  ENRICHMENT PHASE 1 — requests ({PHASE1_WORKERS} threads)", flush=True)
    print(f"{'─' * 80}\n", flush=True)

    p1_ok = p1_skip = p1_decode_fail = 0
    browser_queue = []
    processed_count = 0
    total_articles = len(all_articles)

    ex = ThreadPoolExecutor(max_workers=PHASE1_WORKERS)     # manual management
    try:
        futures = {ex.submit(phase1_worker, item): item for item in all_articles}
        for i, future in enumerate(as_completed(futures), 1):
            processed_count += 1
            try:
                status, item, real_url, extracted = future.result()
                title = item["title"][:50]
                source = item["source"]

                if status == "ok":
                    if extracted is None:
                        raise RuntimeError("Phase 1 returned success without extracted metadata.")
                    update_article(item["google_url"], real_url, extracted, item["article_ref"])
                    desc = (extracted.get("description") or "")[:60]
                    print(f"  [{processed_count}/{total_articles}] ✓ [{source}] {title}", flush=True)
                    print(f"      {extracted['date']}  {desc}", flush=True)
                    p1_ok += 1
                elif status == "skip":
                    print(f"  [{processed_count}/{total_articles}] ⏭️ [skipped] [{source}] {title}", flush=True)
                    p1_skip += 1
                elif status == "decode_fail":
                    print(f"  [{processed_count}/{total_articles}] ✗ [decode failed] [{source}] {title}", flush=True)
                    mark_failed(item["google_url"], None, source, item["title"], "decode_failed")
                    p1_decode_fail += 1
                elif status == "needs_browser":
                    print(f"  [{processed_count}/{total_articles}] ⏳ [→ browser] [{source}] {title}", flush=True)
                    if real_url:
                        item["real_url"] = real_url
                    browser_queue.append(item)

            except Exception as e:
                print(f"  [{processed_count}] ✗ thread error: {e}", flush=True)

            if processed_count % ENRICH_SAVE_INTERVAL == 0:
                save_json(CACHE_FILE, URL_CACHE)
                save_enriched_data()
                print(f"  💾 Saved progress at {processed_count}/{total_articles}", flush=True)

    except KeyboardInterrupt:
        print("\n  ⚡ Phase 1 interrupted — saving cache and enriched data", flush=True)
        save_json(CACHE_FILE, URL_CACHE)
        save_enriched_data()
        ex.shutdown(wait=False, cancel_futures=True)  # instant stop
        print("🛑 Exiting.")
        sys.exit(0)
    else:
        ex.shutdown(wait=True)   # normal completion

    save_json(CACHE_FILE, URL_CACHE)
    save_enriched_data()
    print(f"\n  Phase 1 done — ok={p1_ok}  skip={p1_skip}  "
          f"decode_fail={p1_decode_fail}  browser_queue={len(browser_queue)}", flush=True)

    # ----- Phase 2 (browser) -----
    if browser_queue and not SKIP_BROWSER:
        print(f"\n{'─' * 80}", flush=True)
        print(f"  ENRICHMENT PHASE 2 — browser ({PHASE2_BROWSERS} instances, {TABS_PER_BROWSER} tabs each)", flush=True)
        print(f"  {len(browser_queue)} articles in queue", flush=True)
        print(f"{'─' * 80}\n", flush=True)
        try:
            asyncio.run(phase2_browser(browser_queue))
        except KeyboardInterrupt:
            print("\n  ⚡ Phase 2 interrupted — saving state", flush=True)
            save_json(FAILED_FILE, FAILED)
            save_json(CACHE_FILE, URL_CACHE)
            save_enriched_data()
            print("🛑 Exiting.")
            sys.exit(0)
    elif SKIP_BROWSER and browser_queue:
        print(f"\n  Skipping browser phase — {len(browser_queue)} articles added to failed list", flush=True)
        for item in browser_queue:
            mark_failed(item["google_url"], item.get("real_url"),
                        item["source"], item["title"], "browser_skipped")

    save_json(FAILED_FILE, FAILED)
    save_json(CACHE_FILE, URL_CACHE)
    save_enriched_data()

    # Summary
    fail_by_source = Counter(f["source"] for f in FAILED)
    print(f"\n{'=' * 80}", flush=True)
    print(f"  ENRICHMENT DONE", flush=True)
    print(f"  Success : {p1_ok}", flush=True)
    print(f"  Decode failures : {p1_decode_fail}", flush=True)
    print(f"  Skipped : {p1_skip}", flush=True)
    print(f"  Failed total : {len(FAILED)}", flush=True)
    if fail_by_source:
        print(f"\n  Failed by source (top 10):", flush=True)
        for src, cnt in fail_by_source.most_common(10):
            print(f"    {src:<35} {cnt}", flush=True)
    print(f"\n  Failed list : {FAILED_FILE}", flush=True)
    print(f"  URL cache   : {CACHE_FILE}", flush=True)
    print(f"  Output JSON : {OUTPUT_JSON}", flush=True)
    print("=" * 80, flush=True)

# =============================================================================
#  PHASE 3 – BLOOMBERG PLAYWRIGHT ENRICHMENT
# =============================================================================
# This section contains the Playwright‑based scraper specifically for Bloomberg.
# It will be called after Phase 2 and operates on the enriched JSON file.
# =============================================================================

# ---- Stealth script for Playwright (hides automation) ----
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete navigator.__proto__.webdriver;
window.chrome = {
    runtime: {
        onConnect: undefined,
        onMessage: undefined
    },
    loadTimes: function() {
        return {
            commitLoadTime: Date.now() - Math.random() * 1000,
            finishDocumentLoadTime: Date.now() - Math.random() * 500,
            finishLoadTime: Date.now() - Math.random() * 200,
            firstPaintAfterLoadTime: Date.now() - Math.random() * 100,
            firstPaintTime: Date.now() - Math.random() * 800,
            navigationType: 'Other',
            numProcesses: Math.floor(Math.random() * 5) + 3,
            numRenderProcesses: Math.floor(Math.random() * 3) + 1,
            wasAlternateProtocolAvailable: false,
            wasFetchedViaSpdy: false,
            wasNpnNegotiated: false
        };
    },
    csi: function() {
        return {
            pageT: Date.now() - Math.random() * 1000,
            startE: Date.now() - Math.random() * 2000,
            tran: Math.floor(Math.random() * 20)
        };
    },
    app: {
        isInstalled: false,
        InstallState: {
            DISABLED: 'disabled',
            INSTALLED: 'installed',
            NOT_INSTALLED: 'not_installed'
        }
    }
};
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
            {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''}
        ];
        plugins.length = 3;
        return plugins;
    }
});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'connection', {
    get: () => ({
        effectiveType: '4g',
        rtt: Math.floor(Math.random() * 50) + 50,
        downlink: Math.random() * 10 + 5,
        saveData: false
    })
});
navigator.getBattery = () => Promise.resolve({
    charging: true,
    chargingTime: 0,
    dischargingTime: Infinity,
    level: Math.random() * 0.5 + 0.5
});
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
"""

# ---- Bloomberg helper functions (human‑like interaction) ----
def human_delay_bloomberg(min_sec=1, max_sec=3, sometimes_longer=True):
    delay = random.uniform(min_sec, max_sec)
    if sometimes_longer and random.random() < 0.2:
        delay += random.uniform(2, 5)
    time.sleep(delay)

def human_mouse_move(page):
    try:
        start_x = random.randint(400, 800)
        start_y = random.randint(300, 500)
        movements = random.randint(2, 6)
        for _ in range(movements):
            end_x = random.randint(100, 1100)
            end_y = random.randint(100, 650)
            steps = random.randint(3, 8)
            for step in range(steps):
                progress = (step + 1) / steps
                curr_x = int(start_x + (end_x - start_x) * progress + random.randint(-5, 5))
                curr_y = int(start_y + (end_y - start_y) * progress + random.randint(-5, 5))
                page.mouse.move(curr_x, curr_y)
                time.sleep(random.uniform(0.01, 0.05))
            start_x, start_y = end_x, end_y
            if random.random() < 0.4:
                time.sleep(random.uniform(0.1, 0.4))
    except:
        pass

def human_scroll(page):
    try:
        scroll_actions = random.randint(2, 5)
        for _ in range(scroll_actions):
            distance = random.choice([random.randint(50, 150), random.randint(200, 400), random.randint(100, 250)])
            if random.random() < 0.2:
                distance = -random.randint(50, 150)
            page.mouse.wheel(0, distance)
            pause = random.choice([random.uniform(0.3, 0.8), random.uniform(1.0, 2.5), random.uniform(0.5, 1.2)])
            time.sleep(pause)
        if random.random() < 0.3:
            time.sleep(random.uniform(1.5, 3.0))
    except:
        pass

def human_read_article(page):
    time.sleep(random.uniform(1, 2))
    if random.random() < 0.5:
        human_mouse_move(page)
    human_scroll(page)
    if random.random() < 0.3:
        human_mouse_move(page)
    time.sleep(random.uniform(0.5, 1.5))

def wait_for_captcha(page):
    content = page.content().lower()
    captcha_signs = ["press & hold", "unusual activity", "verify you are human", "captcha", "challenge-platform", "cf-browser-verification"]
    paywall_signs = ["subscribe to continue", "subscription required", "become a subscriber", "unlock this article", "premium content", "sign in to read", "create an account"]
    if any(sign in content for sign in paywall_signs):
        return
    if not any(sign in content for sign in captcha_signs):
        return
    print("\n⚠️  CAPTCHA REQUIRED! Solve it in the browser window.")
    input("   >>> Press Enter when done: ")
    time.sleep(2)

def handle_captcha(page):
    content = page.content().lower()
    if "press & hold" not in content and "unusual activity" not in content:
        return True
    print("  CAPTCHA detected, attempting to auto‑solve...")
    selectors = ["div[role='button'][aria-label*='Press']", "div[tabindex='0'][role='button']", "button:has-text('PRESS & HOLD')"]
    for selector in selectors:
        try:
            button = page.wait_for_selector(selector, timeout=3000)
            if button:
                button.hover()
                time.sleep(random.uniform(0.5, 1))
                page.mouse.down()
                time.sleep(random.uniform(5, 7))
                page.mouse.up()
                time.sleep(3)
                if "press & hold" not in page.content().lower():
                    print("  CAPTCHA solved!")
                    return True
        except:
            continue
    return False

# ---- Bloomberg‑specific URL decoder (reuses the same logic as Phase 2 but with different library) ----
def decode_google_news_url_bloomberg(url):
    """Decode a Google News RSS URL to the real article URL (Bloomberg version)."""
    return decode_google_url(url)

# ---- Bloomberg metadata extraction (similar to Phase 2 but also extracts author) ----
def extract_metadata_bloomberg(soup):
    """Extract title, date, description, author from page soup (Bloomberg‑specific)."""
    meta = {"title": "", "published_at": "", "author": "", "description": "", "extraction_source": ""}
    # JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("@type", "")
                if isinstance(item_type, list):
                    item_type = " ".join(item_type)
                if "Article" not in str(item_type) and "NewsArticle" not in str(item_type):
                    continue
                if not meta["title"]:
                    meta["title"] = item.get("headline", "") or item.get("name", "")
                if not meta["published_at"]:
                    raw_date = item.get("datePublished") or item.get("dateCreated", "")
                    if raw_date:
                        # reuse the same parse_datetime from Phase 2
                        parsed = parse_dt(raw_date)  # uses the global parse_dt
                        meta["published_at"] = parsed or raw_date
                if not meta["description"]:
                    meta["description"] = item.get("description", "")
                if not meta["author"]:
                    author = item.get("author", "")
                    if isinstance(author, dict):
                        author = author.get("name", "")
                    elif isinstance(author, list) and author:
                        author = author[0].get("name", "") if isinstance(author[0], dict) else str(author[0])
                    meta["author"] = str(author)
                if meta["title"] and meta["published_at"]:
                    meta["extraction_source"] = "jsonld"
                    return meta
        except:
            continue
    # OpenGraph / meta fallbacks
    if not meta["title"]:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            meta["title"] = og_title["content"]
            meta["extraction_source"] = meta["extraction_source"] or "og"
    if not meta["description"]:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            meta["description"] = og_desc["content"]
        else:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                meta["description"] = meta_desc["content"]
    if not meta["published_at"]:
        for prop in ["article:published_time", "og:published_time"]:
            tag = soup.find("meta", property=prop)
            if tag and tag.get("content"):
                parsed = parse_dt(tag["content"])
                meta["published_at"] = parsed or tag["content"]
                if parsed:
                    meta["extraction_source"] = meta["extraction_source"] or f"meta-{prop}"
                break
    if not meta["author"]:
        for tag_attrs in [("meta", {"name": "author"}), ("meta", {"property": "article:author"})]:
            tag = soup.find(tag_attrs[0], attrs=tag_attrs[1])
            if tag and tag.get("content"):
                meta["author"] = tag["content"]
                break
    return meta

# ---- Load and filter Bloomberg articles from enriched JSON ----
def load_and_filter_bloomberg_articles(filepath):
    """
    Load the enriched JSON and return a list of Bloomberg articles that need enrichment.
    Each item contains a reference to the article dictionary for in‑place update.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles_to_scrape = []
    for source, art_list in data.get("articles", {}).items():
        if source.lower() != "bloomberg.com":
            continue
        for art in art_list:
            if art.get("description"):
                continue
            google_url = art.get("link", "")
            if not google_url or "news.google.com" not in google_url:
                continue
            articles_to_scrape.append({
                "source": source,
                "google_url": google_url,
                "title": art.get("title", ""),
                "art_dict": art,
                "data": data,
            })
    return data, articles_to_scrape

# ---- Save updated Bloomberg articles back to file ----
def save_bloomberg_enrichment(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---- Main Bloomberg enrichment function ----
def enrich_bloomberg():
    """
    Phase 3: Enrich Bloomberg articles using Playwright with human-like behavior.

    Why this phase is separate:
    - Bloomberg often requires a much more realistic browsing experience.
    - A regular HTTP request or even a simple browser session may not be enough.
    - Playwright lets us control a real browser tab and interact with the page more naturally.

    What the function does:
    - It loads the enriched JSON file and looks only for Bloomberg entries that still need data.
    - It decodes the Google News link into the real Bloomberg article URL.
    - It opens the article in a real browser, waits, scrolls, and extracts metadata.
    - If successful, it writes the new information back into the JSON file.
    """
    if not os.path.exists(BLOOMBERG_INPUT_FILE):
        print(f"❌ Bloomberg input file {BLOOMBERG_INPUT_FILE} not found. Skipping Phase 3.")
        return

    print("\n" + "=" * 80)
    print("  PHASE 3 – BLOOMBERG PLAYWRIGHT ENRICHMENT")
    print("=" * 80)
    print(f"📄 Loading {BLOOMBERG_INPUT_FILE}...")
    data, articles = load_and_filter_bloomberg_articles(BLOOMBERG_INPUT_FILE)
    if not articles:
        print("✅ No Bloomberg articles to enrich (all already have descriptions).")
        return

    print(f"🔍 Found {len(articles)} Bloomberg articles to scrape.\n")

    # Check if Playwright is installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed. Install with: pip install playwright && playwright install chromium")
        return

    print(f"🖥️  Starting Playwright browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080"
            ]
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()

        # Warm‑up
        try:
            page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=15000)
            human_delay_bloomberg(2, 4)
            human_mouse_move(page)
        except:
            pass

        success = 0
        failed = 0

        for i, item in enumerate(articles, 1):
            title = item["title"][:60]
            google_url = item["google_url"]
            print(f"\n[{i}/{len(articles)}] {title}...")

            # Decode URL just before scraping
            print("  🔓 Decoding URL...")
            decoded_url = decode_google_news_url_bloomberg(google_url)
            if not decoded_url:
                print("  ❌ Failed to decode – skipping article.")
                failed += 1
                continue
            print(f"  ✓ Decoded: {decoded_url[:80]}...")
            item["decoded_url"] = decoded_url

            try:
                page.goto(decoded_url, wait_until="domcontentloaded", timeout=60000)
                human_delay_bloomberg(3, 5)
                wait_for_captcha(page)
                handle_captcha(page)
                human_read_article(page)
                soup = BeautifulSoup(page.content(), "lxml")
                metadata = extract_metadata_bloomberg(soup)

                art_dict = item["art_dict"]
                if metadata.get("published_at"):
                    art_dict["original_published_at"] = art_dict.get("published", "")
                    art_dict["published_at"] = metadata["published_at"]
                    art_dict["description"] = metadata.get("description", "") or art_dict.get("description", "")
                    art_dict["author"] = metadata.get("author", "")
                    art_dict["extraction_source"] = metadata.get("extraction_source", "")
                    art_dict["decoded_url"] = decoded_url
                    art_dict.pop("published", None)
                    art_dict.pop("query", None)

                    save_bloomberg_enrichment(BLOOMBERG_INPUT_FILE, item["data"])
                    print(f"  ✓ {metadata['published_at']} | {metadata.get('description', '')[:60]}...")
                    success += 1
                else:
                    print("  ✗ No date found")
                    failed += 1

                delay = random.uniform(3, 6)
                if i % random.randint(5, 10) == 0:
                    print("  (taking a short break...)")
                    delay += random.uniform(8, 15)
                if random.random() < 0.1:
                    delay = random.uniform(1, 2)
                elif random.random() < 0.1:
                    delay += random.uniform(5, 10)
                time.sleep(delay)

            except KeyboardInterrupt:
                print("\n  ⛔ Stopped by user")
                break
            except Exception as e:
                print(f"  ✗ error: {str(e)[:80]}")
                failed += 1
                human_delay_bloomberg(2, 5)

        context.close()
        browser.close()

    print(f"\n{'='*50}")
    print(f"  BLOOMBERG PHASE 3 DONE")
    print(f"  SUCCESS: {success}")
    print(f"  FAILED:  {failed}")
    print(f"  File updated: {BLOOMBERG_INPUT_FILE}")
    print(f"{'='*50}")

# -----------------------------------------------------------------------------
#  MAIN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    random.seed(42)

    # Load source list from file or fallback
    if os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list) and len(data) > 0 and 'source' in data[0]:
                SOURCES = [item['source'] for item in data]
            else:
                SOURCES = data
        print(f"Loaded {len(SOURCES)} sources from {SOURCES_FILE}")
    else:
        print(f"⚠️ File {SOURCES_FILE} not found. Using default list.")
        SOURCES = ["bbc", "cnn", "reuters", "ap", "npr"]

    # Phase 1 – RSS Scraping
    scrape_google_news(
        sources=SOURCES,
        after_date=AFTER_DATE,
        before_date=BEFORE_DATE,
        articles_per_source=ARTICLES_PER_SOURCE,
        max_sources=MAX_SOURCES,
        output_file=RSS_OUTPUT_FILE,
        save_interval=RSS_SAVE_INTERVAL,
        workers=RSS_WORKERS,
        resume=True
    )

    # Phase 2 – Enrichment (skips Bloomberg)
    run_enrichment()

    # Phase 3 – Bloomberg Playwright enrichment
    enrich_bloomberg()
