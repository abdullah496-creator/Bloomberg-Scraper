"""
PostgreSQL Database Helper Module
Use this module to directly save scraped articles or query them from PostgreSQL.
"""

import os
from typing import Dict, List, Any
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import psycopg2
from psycopg2.extras import execute_values

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_USER = os.getenv("DB_USER", "postgres")
DB_NAME = os.getenv("DB_NAME", "news_db")
DB_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("PGPASSWORD")


def get_connection():
    """Returns a psycopg2 database connection."""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME
    )


import hashlib

def init_db():
    """Ensure table and indexes exist."""
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
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(create_table_query)
        conn.commit()
    finally:
        conn.close()


def save_articles_to_postgres(articles_list: List[Dict[str, Any]]):
    """
    Directly insert a list of article dicts into PostgreSQL.
    Skips duplicates based on unique 'link_hash' (SHA-256).
    """
    if not articles_list:
        return

    records = []
    for art in articles_list:
        link = art.get("link")
        if not link:
            continue
        link_hash = hashlib.sha256(link.encode("utf-8", errors="ignore")).hexdigest()
        records.append((
            link_hash,
            art.get("title"),
            link,
            art.get("google_link"),
            art.get("source"),
            art.get("query") or art.get("category"),
            art.get("published_at") or art.get("published"),
            art.get("original_published_at") or art.get("published"),
            art.get("description"),
            art.get("author"),
            art.get("time_source"),
            art.get("time_tier")
        ))

    insert_sql = """
    INSERT INTO news_articles (
        link_hash,
        title, link, google_link, source, category_query,
        published_at, original_published_at, description,
        author, time_source, time_tier
    ) VALUES %s
    ON CONFLICT (link_hash) DO NOTHING;
    """

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, records)
        conn.commit()
    finally:
        conn.close()
