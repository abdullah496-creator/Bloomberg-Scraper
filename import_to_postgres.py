"""
PostgreSQL Importer for Scraped News Articles
This script creates a PostgreSQL database & table and imports your JSON news data in fast batches.
"""

import json
import os
import sys
from getpass import getpass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import execute_values
except ImportError:
    print("\n[ERROR] psycopg2 library nahi mili!")
    print("Pehle ye command run karein:")
    print("    pip install psycopg2-binary python-dotenv\n")
    sys.exit(1)


# =============================================================================
# DATABASE CONFIGURATION (Loaded securely from .env or environment variables)
# =============================================================================
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_NAME = os.getenv("DB_NAME", "news_db")          # Is naam ka database automatically ban jayega
DB_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD")

# File to import (pehlay enriched check karega, agar na mili to raw file)
ENRICHED_FILE = "google_news_articles_enriched.json"
RAW_FILE = "google_news_articles.json"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================
def get_db_password():
    """Password prompt agar .env ya environment variables me set na ho."""
    global DB_PASSWORD
    if not DB_PASSWORD:
        print("=" * 60)
        print("PostgreSQL password enter karein (ya .env file me set karein):")
        DB_PASSWORD = getpass("Password: ")
        print("=" * 60)
    return DB_PASSWORD


def create_database_if_not_exists(password):
    """PostgreSQL ke default 'postgres' database se connect ho kar 'news_db' create karta hai."""
    print(f"Connecting to PostgreSQL server at {DB_HOST}:{DB_PORT} as '{DB_USER}'...")
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=password,
            dbname="postgres"  # default maintenance database
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Check if database exists
        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (DB_NAME,))
        exists = cur.fetchone()

        if not exists:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
            print(f"✓ Database '{DB_NAME}' kamyabi se create ho gaya!")
        else:
            print(f"✓ Database '{DB_NAME}' pehle se mojood hai.")

        cur.close()
        conn.close()
    except Exception as e:
        print(f"\n[ERROR] PostgreSQL server se connect hone me masla aya:")
        print(f"Detail: {e}")
        print("\nPossible solutions:")
        print("1. Check karein ke PostgreSQL ki service Windows Services me 'Running' hai.")
        print("2. Apna password theek enter karein.")
        sys.exit(1)


import hashlib


def init_table(conn):
    """Table aur indexes create karta hai agar na hon."""
    create_table_query = """
    CREATE TABLE IF NOT EXISTS news_articles (
        id SERIAL PRIMARY KEY,
        link_hash VARCHAR(64) UNIQUE,
        title TEXT,
        link TEXT,
        google_link TEXT,
        source VARCHAR(255),
        category_query VARCHAR(255),
        published_at VARCHAR(255),
        original_published_at VARCHAR(255),
        description TEXT,
        author VARCHAR(255),
        time_source VARCHAR(100),
        time_tier VARCHAR(100),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_news_source ON news_articles(source);
    CREATE INDEX IF NOT EXISTS idx_news_category ON news_articles(category_query);
    CREATE INDEX IF NOT EXISTS idx_news_published_at ON news_articles(published_at);
    """
    with conn.cursor() as cur:
        cur.execute(create_table_query)
    conn.commit()
    print("✓ Table 'news_articles' aur indexes tayar hain.")


def load_articles():
    """JSON file se articles load karta hai."""
    file_path = None
    if os.path.exists(ENRICHED_FILE):
        file_path = ENRICHED_FILE
    elif os.path.exists(RAW_FILE):
        file_path = RAW_FILE
    else:
        print(f"[ERROR] Koi JSON file nahi mili ({ENRICHED_FILE} ya {RAW_FILE})!")
        sys.exit(1)

    print(f"Reading JSON file: {file_path} ... (ye 10-20 seconds le sakta hai)")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles_data = data.get("articles", {})
    records = []

    # Flatten categories/sources into a single list
    for category_or_source, items in articles_data.items():
        if isinstance(items, list):
            for art in items:
                # Handle both raw RSS format and enriched format
                title = art.get("title")
                link = art.get("link")
                if not link:
                    continue

                link_hash = hashlib.sha256(link.encode("utf-8", errors="ignore")).hexdigest()
                google_link = art.get("google_link")
                source = art.get("source")
                category_query = art.get("query") or category_or_source
                published_at = art.get("published_at") or art.get("published")
                orig_published = art.get("original_published_at") or art.get("published")
                description = art.get("description")
                author = art.get("author")
                time_source = art.get("time_source")
                time_tier = art.get("time_tier")

                records.append((
                    link_hash,
                    title,
                    link,
                    google_link,
                    source,
                    category_query,
                    published_at,
                    orig_published,
                    description,
                    author,
                    time_source,
                    time_tier
                ))

    print(f"✓ Total {len(records):,} articles file se load hue.")
    return records


def bulk_insert_articles(conn, records, batch_size=2000):
    """
    Articles ko fast batch me insert karta hai using psycopg2 execute_values.
    Duplicate links skip ho jate hain (ON CONFLICT (link_hash) DO NOTHING).
    """
    insert_sql = """
    INSERT INTO news_articles (
        link_hash,
        title,
        link,
        google_link,
        source,
        category_query,
        published_at,
        original_published_at,
        description,
        author,
        time_source,
        time_tier
    ) VALUES %s
    ON CONFLICT (link_hash) DO NOTHING;
    """

    total = len(records)
    print(f"\nPostgreSQL me data insert hona shuru ho raha hai (Batch size: {batch_size})...")

    with conn.cursor() as cur:
        for i in range(0, total, batch_size):
            batch = records[i:i + batch_size]
            execute_values(cur, insert_sql, batch)
            conn.commit()
            processed = min(i + batch_size, total)
            percent = (processed / total) * 100
            print(f"  Processed {processed:,} / {total:,} ({percent:.1f}%) articles...", end="\r", flush=True)

    print(f"\n✓ Tamam {total:,} records process ho gaye!")


def get_total_count(conn):
    """Check total articles in DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM news_articles;")
        return cur.fetchone()[0]


# =============================================================================
# MAIN EXECUTION
# =============================================================================
def main():
    print("=" * 60)
    print("  NEWS JSON TO POSTGRESQL IMPORTER")
    print("=" * 60)

    password = get_db_password()

    # Step 1: Ensure database exists
    create_database_if_not_exists(password)

    # Step 2: Connect to news_db
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=password,
        dbname=DB_NAME
    )

    try:
        # Step 3: Ensure table and indexes exist
        init_table(conn)

        # Step 4: Load JSON articles
        records = load_articles()

        # Step 5: Fast batch insert
        bulk_insert_articles(conn, records)

        # Step 6: Summary
        final_count = get_total_count(conn)
        print("\n" + "=" * 60)
        print(f"  MUBARAK HO! PostgreSQL me total {final_count:,} articles mojood hain.")
        print(f"  Database Name : {DB_NAME}")
        print(f"  Table Name    : news_articles")
        print("=" * 60)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
