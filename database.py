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
        "was", "are", "be", "as", "at", "so", "we", "he", "she", "they",
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
    Create tables if they don't exist, then safely add any missing columns.
    This means you NEVER need to delete posts.db when we add new columns.
    Old data is always preserved.
    """
    async with aiosqlite.connect(DB_NAME) as db:

        # Create posts table (base structure)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                content_type     TEXT NOT NULL,
                text             TEXT,
                file_id          TEXT,
                status           TEXT DEFAULT 'pending'
            )
        """)

        # Create logs table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                action     TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
        """)

        await db.commit()

        # ── Safe migration: add columns that might not exist yet ──
        # We try to add each column; if it already exists, SQLite throws
        # an error which we silently ignore. This way old databases are
        # upgraded automatically without losing any data.
        new_columns = [
            ("normalized_text", "TEXT"),
            ("hash",            "TEXT"),
            ("similarity_flag", "TEXT"),
            ("similar_post_id", "INTEGER"),
            ("channel_msg_id",  "INTEGER"),
        ]

        for col_name, col_type in new_columns:
            try:
                await db.execute(
                    f"ALTER TABLE posts ADD COLUMN {col_name} {col_type}"
                )
                await db.commit()
            except Exception:
                # Column already exists — that's fine, skip it
                pass


# ────────────────────────────────────────────
# POSTS
# ────────────────────────────────────────────

async def add_post(user_id: int, content_type: str, text: str, file_id: str = None) -> int:
    norm = normalize_text(text or "")
    h = make_hash(norm)
    similarity_flag = None
    similar_post_id = None

    async with aiosqlite.connect(DB_NAME) as db:

        # ── Duplicate check ──────────────────────────────────────
        # Only compare against PUBLISHED posts.
        # Pending or rejected posts don't count — they were never
        # confirmed as real news, so a new submission of the same
        # text is perfectly valid.
        cursor = await db.execute(
            "SELECT id FROM posts WHERE hash = ? AND status = 'published' LIMIT 1",
            (h,)
        )
        duplicate_row = await cursor.fetchone()

        if duplicate_row:
            similar_post_id = duplicate_row[0]
            similarity_flag = "DUPLICATE"

        else:
            # ── Similarity check ─────────────────────────────────
            # Again, only compare against published posts
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
                normalized_text, hash, similarity_flag, similar_post_id)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (user_id, content_type, text, file_id, norm, h, similarity_flag, similar_post_id)
        )
        await db.commit()
        return cursor.lastrowid


async def get_post(post_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return await cursor.fetchone()


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