"""
Check and view articles stored in PostgreSQL (news_db).
Run: python check_db.py
"""

import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import psycopg2
from db import DB_HOST, DB_PORT, DB_USER, DB_NAME, DB_PASSWORD

def main():
    print("=" * 65)
    print("  POSTGRESQL NEWS DATABASE VIEWER")
    print("=" * 65)

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME
    )
    cur = conn.cursor()

    # 1. Total Articles
    cur.execute("SELECT COUNT(*) FROM news_articles;")
    total = cur.fetchone()[0]
    print(f"\n📊 Total Articles in Database: {total:,}")

    # 2. Top 5 Sources
    cur.execute("""
        SELECT COALESCE(source, 'Unknown') AS src, COUNT(*) AS count
        FROM news_articles
        GROUP BY src
        ORDER BY count DESC
        LIMIT 5;
    """)
    print("\n🏆 Top 5 News Sources:")
    for row in cur.fetchall():
        print(f"   • {row[0]}: {row[1]:,} articles")

    # 3. Latest 3 Sample Articles
    cur.execute("""
        SELECT title, source, published_at, link
        FROM news_articles
        ORDER BY id DESC
        LIMIT 3;
    """)
    print("\n📰 Latest 3 Articles Sample:")
    for i, (title, src, pub, link) in enumerate(cur.fetchall(), 1):
        print(f"\n   [{i}] Source: {src} | Date: {pub}")
        print(f"       Title: {title[:90]}...")
        print(f"       Link : {link[:80]}...")

    cur.close()
    conn.close()
    print("\n" + "=" * 65)
    print("  Database is connected and working perfectly!")
    print("=" * 65)

if __name__ == "__main__":
    main()
