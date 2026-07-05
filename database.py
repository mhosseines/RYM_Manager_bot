import hashlib
import re
import aiosqlite
from datetime import datetime

DB_NAME = "posts.db"


# ────────────────────────────────────────────
# TEXT UTILITIES
# ────────────────────────────────────────────

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_hash(normalized_text: str) -> str:
    return hashlib.md5(normalized_text.encode("utf-8")).hexdigest()


def compute_similarity(text_a: str, text_b: str) -> float:
    if not text_a or not text_b:
        return 0.0

    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "it", "this", "that",
        "was", "are", "be", "as", "so", "we", "he", "she", "they",
        "his", "her", "our", "its", "not", "no", "up", "out", "if", "do",
        "did", "has", "had", "have", "can", "will", "just", "been", "also",
        "than", "then", "when", "what", "which", "who", "how", "all", "each"
    }

    words_a = set(text_a.split()) - stop_words
    words_b = set(text_b.split()) - stop_words

    if not words_a or not words_b:
        return 0.0

    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ────────────────────────────────────────────
# DATABASE SETUP  —  with safe migration
# ────────────────────────────────────────────

async def init_db():
    """
    Creates all tables and safely adds any missing columns.
    Running this on an existing database will NOT delete any data.
    """
    async with aiosqlite.connect(DB_NAME) as db:

        # ── posts table ──────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER,
                content_type     TEXT NOT NULL,
                text             TEXT,
                file_id          TEXT,
                status           TEXT DEFAULT 'pending'
            )
        """)

        # ── sources table (new in Phase 1.3) ─────────────────────
        # Stores the list of RSS feeds to poll
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                type     TEXT NOT NULL DEFAULT 'rss',
                url      TEXT NOT NULL UNIQUE,
                active   INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 5
            )
        """)

        # ── logs table ───────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                action     TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
        """)

        await db.commit()

        # ── Safe migration: add columns that may not exist yet ───
        # If the column already exists, the ALTER TABLE throws an error
        # which we catch and ignore. This is safe and standard practice.
        new_columns = [
            ("normalized_text", "TEXT"),
            ("hash",            "TEXT"),
            ("similarity_flag", "TEXT"),
            ("similar_post_id", "INTEGER"),
            ("channel_msg_id",  "INTEGER"),
            # Phase 1.3 additions:
            ("source_type",     "TEXT DEFAULT 'user'"),   # "user" or "rss"
            ("source_name",     "TEXT"),                  # e.g. "Zoomit"
            ("source_url",      "TEXT"),                  # direct link to article
        ]

        for col_name, col_definition in new_columns:
            try:
                await db.execute(
                    f"ALTER TABLE posts ADD COLUMN {col_name} {col_definition}"
                )
                await db.commit()
            except Exception:
                pass  # Column already exists — no action needed


# ────────────────────────────────────────────
# SOURCES  (RSS feed management)
# ────────────────────────────────────────────

async def add_source(name: str, url: str, priority: int = 5) -> int:
    """Add a new RSS source. Returns its ID."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO sources (name, type, url, active, priority) VALUES (?, 'rss', ?, 1, ?)",
            (name, url, priority)
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_sources():
    """Return all active RSS sources, sorted by priority (lowest number = highest priority)."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sources WHERE active = 1 ORDER BY priority ASC"
        )
        return await cursor.fetchall()


async def set_source_active(source_id: int, active: bool):
    """Enable or disable an RSS source without deleting it."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE sources SET active = ? WHERE id = ?",
            (1 if active else 0, source_id)
        )
        await db.commit()


# ────────────────────────────────────────────
# POSTS
# ────────────────────────────────────────────

async def add_post(
    content_type: str,
    text: str,
    user_id: int = None,
    file_id: str = None,
    source_type: str = "user",
    source_name: str = None,
    source_url: str = None,
) -> int:
    """
    Insert a new post (from a user OR from RSS).
    Automatically runs duplicate and similarity checks.
    Returns the new post's ID.
    """
    norm = normalize_text(text or "")
    h    = make_hash(norm)
    similarity_flag = None
    similar_post_id = None

    async with aiosqlite.connect(DB_NAME) as db:

        # ── Duplicate check (only against published posts) ───────
        cursor = await db.execute(
            "SELECT id FROM posts WHERE hash = ? AND status = 'published' LIMIT 1",
            (h,)
        )
        duplicate_row = await cursor.fetchone()

        if duplicate_row:
            similar_post_id = duplicate_row[0]
            similarity_flag = "DUPLICATE"

        else:
            # ── Similarity check (only against published posts) ──
            cursor = await db.execute(
                """SELECT id, normalized_text FROM posts
                   WHERE status = 'published'
                   ORDER BY id DESC LIMIT 50"""
            )
            recent_posts = await cursor.fetchall()

            SIMILARITY_THRESHOLD = 0.60

            for existing_id, existing_norm in recent_posts:
                if not existing_norm:
                    continue
                score = compute_similarity(norm, existing_norm)
                if score >= SIMILARITY_THRESHOLD:
                    percent = int(score * 100)
                    similar_post_id = existing_id
                    similarity_flag = f"SIMILAR:{percent}"
                    break

        cursor = await db.execute(
            """INSERT INTO posts
               (user_id, content_type, text, file_id, status,
                normalized_text, hash, similarity_flag, similar_post_id,
                source_type, source_name, source_url)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, content_type, text, file_id,
             norm, h, similarity_flag, similar_post_id,
             source_type, source_name, source_url)
        )
        await db.commit()
        return cursor.lastrowid


async def get_post(post_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return await cursor.fetchone()


async def hash_already_seen(h: str) -> bool:
    """
    Quick check: has this exact hash appeared in ANY post before?
    (pending, approved, published, rejected — any status)
    Used by the RSS fetcher to avoid re-submitting the same article
    on every poll cycle.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id FROM posts WHERE hash = ? LIMIT 1", (h,)
        )
        row = await cursor.fetchone()
        return row is not None


async def update_status(post_id: int, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET status = ? WHERE id = ?", (status, post_id)
        )
        await db.commit()


async def save_channel_msg_id(post_id: int, msg_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET channel_msg_id = ? WHERE id = ?", (msg_id, post_id)
        )
        await db.commit()


# ────────────────────────────────────────────
# LOGS
# ────────────────────────────────────────────

async def log_action(post_id: int, action: str):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO logs (post_id, action, timestamp) VALUES (?, ?, ?)",
            (post_id, action, timestamp)
        )
        await db.commit()