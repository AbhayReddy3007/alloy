"""
AlloyDB connection and setup verification script.
"""

import os
import sys
import time
import urllib.parse

# ── Load .env if present ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Required library check ───────────────────────────────────────────────────
try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2-binary not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ── Load credentials from .env ───────────────────────────────────────────────
raw_password = os.getenv("ALLOYDB_PASSWORD")
ip = os.getenv("ALLOYDB_HOST")
user = os.getenv("ALLOYDB_USER", "postgres")
db = os.getenv("ALLOYDB_DB", "postgres")

if not raw_password or not ip:
    print("ERROR: Missing ALLOYDB_PASSWORD or ALLOYDB_HOST in .env")
    sys.exit(1)

encoded_password = urllib.parse.quote_plus(raw_password)

admin_url = f"postgresql://{user}:{encoded_password}@{ip}:5432/{db}"

# ── File paths ───────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETUP_SQL = os.path.join(SCRIPT_DIR, "setup_alloydb.sql")

# ─────────────────────────────────────────────────────────────────────────────

def ok(msg):   print(f"  [OK]   {msg}")
def fail(msg): print(f"  [FAIL] {msg}")
def info(msg): print(f"  [INFO] {msg}")

def step(n, title):
    print(f"\nStep {n}: {title}")
    print("  " + "-" * 50)

def _mask(url: str) -> str:
    import re
    return re.sub(r'(:)[^:@]+(@)', r'\1****\2', url)

def _url_with_db(url: str, dbname: str) -> str:
    import re
    return re.sub(r'(/)[^/?]+(\?|$)', rf'\1{dbname}\2', url)

# ─────────────────────────────────────────────────────────────────────────────

def check_url():
    step(1, "admin_url env var")

    url = admin_url
    if not url:
        fail("admin_url is not set.")
        sys.exit(1)

    url = url.replace("postgresql+asyncpg://", "postgresql://")
    ok(f"admin_url is set → {_mask(url)}")

    return url

# ─────────────────────────────────────────────────────────────────────────────

def ensure_database(url):
    step(2, "Ensure database exists")

    import re
    m = re.search(r'/([^/?]+)(\?|$)', url)
    dbname = m.group(1) if m else "postgres"

    admin_conn_url = _url_with_db(url, "postgres")

    try:
        conn = psycopg2.connect(admin_conn_url, connect_timeout=10)
        conn.autocommit = True

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (dbname,)
            )
            exists = cur.fetchone()

        if exists:
            ok(f"Database '{dbname}' already exists")
        else:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{dbname}"')
            ok(f"Database '{dbname}' created")

        conn.close()

    except psycopg2.OperationalError as e:
        fail(f"Could not connect: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────

def check_connect(url):
    step(3, "psycopg2 connection")

    try:
        t0 = time.time()
        conn = psycopg2.connect(url, connect_timeout=10)
        elapsed = (time.time() - t0) * 1000
        ok(f"Connected in {elapsed:.0f} ms")
        return conn

    except psycopg2.OperationalError as e:
        fail(f"Connection failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────

def check_vector_extension(conn):
    step(4, "pgvector extension")

    with conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()

    if row:
        ok("vector extension exists")
        return

    info("Creating vector extension...")
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        ok("vector enabled")

    except Exception as e:
        conn.rollback()
        fail(f"Extension failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────

def check_table(conn):
    step(5, "embeddings table")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_name = 'embeddings'
            )
        """)
        exists = cur.fetchone()[0]

    if exists:
        ok("embeddings table exists")
        return

    info("Running setup_alloydb.sql...")

    if not os.path.exists(SETUP_SQL):
        fail("setup_alloydb.sql not found")
        sys.exit(1)

    with open(SETUP_SQL) as f:
        sql = f.read()

    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        ok("Table created")

    except Exception as e:
        conn.rollback()
        fail(f"Setup failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────

def check_roundtrip(conn):
    step(6, "Vector test")

    dummy_id = "__test_vector__"
    dummy_vec = "[" + ",".join(["0.001"] * 768) + "]"

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO embeddings (unique_id, sub_id, text, embedding)
                VALUES (%s, '__test__', 'test', %s::vector)
                ON CONFLICT (unique_id) DO NOTHING
            """, (dummy_id, dummy_vec))

            conn.commit()
            ok("Insert OK")

            cur.execute("""
                SELECT unique_id
                FROM embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT 1
            """, (dummy_vec,))

            row = cur.fetchone()
            ok(f"Query OK: {row}")

            cur.execute("DELETE FROM embeddings WHERE unique_id = %s", (dummy_id,))
            conn.commit()
            ok("Delete OK")

    except Exception as e:
        conn.rollback()
        fail(f"Roundtrip failed: {e}")
        sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────

def check_indexes(conn):
    step(7, "Indexes")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename='embeddings'
        """)

        rows = cur.fetchall()

    if rows:
        for r in rows:
            ok(r[0])
    else:
        info("No indexes found")

# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("AIRA — AlloyDB Verification")
    print("=" * 60)

    url = check_url()
    ensure_database(url)

    conn = check_connect(url)

    try:
        check_vector_extension(conn)
        check_table(conn)
        check_roundtrip(conn)
        check_indexes(conn)

    finally:
        conn.close()

    print("\n✅ All checks passed!")

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
