import hashlib
import re
import aiosqlite
from datetime import datetime, date

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
# DATABASE SETUP  —  safe migration
# ────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:

        # posts
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

        # sources (RSS feeds)
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

        # logs
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                action     TEXT NOT NULL,
                timestamp  TEXT NOT NULL
            )
        """)

        # blocked users  ← NEW
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id    INTEGER PRIMARY KEY,
                reason     TEXT,
                blocked_at TEXT NOT NULL
            )
        """)

        await db.commit()

        # Safe column migrations — never deletes data
        new_columns = [
            ("normalized_text", "TEXT"),
            ("hash",            "TEXT"),
            ("similarity_flag", "TEXT"),
            ("similar_post_id", "INTEGER"),
            ("channel_msg_id",  "INTEGER"),
            ("source_type",     "TEXT DEFAULT 'user'"),
            ("source_name",     "TEXT"),
            ("source_url",      "TEXT"),
        ]
        for col_name, col_definition in new_columns:
            try:
                await db.execute(
                    f"ALTER TABLE posts ADD COLUMN {col_name} {col_definition}"
                )
                await db.commit()
            except Exception:
                pass


# ────────────────────────────────────────────
# BLOCKED USERS
# ────────────────────────────────────────────

async def block_user(user_id: int, reason: str = ""):
    """Add a user to the blocked list."""
    blocked_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id, reason, blocked_at) VALUES (?, ?, ?)",
            (user_id, reason, blocked_at)
        )
        await db.commit()


async def unblock_user(user_id: int):
    """Remove a user from the blocked list."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM blocked_users WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def is_user_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT user_id FROM blocked_users WHERE user_id = ?", (user_id,)
        )
        return await cursor.fetchone() is not None


# ────────────────────────────────────────────
# RATE LIMITING  —  1 submission per minute per user
# Stored in memory (resets when bot restarts, which is fine)
# ────────────────────────────────────────────

# How many seconds a user must wait between submissions
RATE_LIMIT_SECONDS = 60

# Dict:  user_id  →  timestamp of their last submission
_last_submission: dict[int, datetime] = {}


def check_rate_limit(user_id: int) -> int:
    """
    Check if the user is allowed to post right now.
    Returns 0 if allowed, or the number of seconds they must still wait.
    Always call BEFORE inserting a post.
    """
    now = datetime.utcnow()
    last = _last_submission.get(user_id)

    if last is None:
        return 0  # First time posting — always allowed

    elapsed = (now - last).total_seconds()
    remaining = RATE_LIMIT_SECONDS - elapsed

    return max(0, int(remaining))


def record_submission(user_id: int):
    """
    Record that this user just submitted a post.
    Call this AFTER successfully inserting the post.
    """
    _last_submission[user_id] = datetime.utcnow()


# ────────────────────────────────────────────
# SOURCES
# ────────────────────────────────────────────

async def add_source(name: str, url: str, priority: int = 5) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO sources (name, type, url, active, priority) VALUES (?, 'rss', ?, 1, ?)",
            (name, url, priority)
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_sources():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sources WHERE active = 1 ORDER BY priority ASC"
        )
        return await cursor.fetchall()


async def set_source_active(source_id: int, active: bool):
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
    norm = normalize_text(text or "")
    h    = make_hash(norm)
    similarity_flag = None
    similar_post_id = None

    async with aiosqlite.connect(DB_NAME) as db:

        # Duplicate check — only published posts count
        cursor = await db.execute(
            "SELECT id FROM posts WHERE hash = ? AND status = 'published' LIMIT 1", (h,)
        )
        duplicate_row = await cursor.fetchone()

        if duplicate_row:
            similar_post_id = duplicate_row[0]
            similarity_flag = "DUPLICATE"
        else:
            cursor = await db.execute(
                """SELECT id, normalized_text FROM posts
                   WHERE status = 'published'
                   ORDER BY id DESC LIMIT 50"""
            )
            recent_posts = await cursor.fetchall()

            for existing_id, existing_norm in recent_posts:
                if not existing_norm:
                    continue
                score = compute_similarity(norm, existing_norm)
                if score >= 0.60:
                    similar_post_id = existing_id
                    similarity_flag = f"SIMILAR:{int(score * 100)}"
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
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id FROM posts WHERE hash = ? LIMIT 1", (h,)
        )
        return await cursor.fetchone() is not None


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
# STATS  (for /status command)
# ────────────────────────────────────────────

async def get_stats() -> dict:
    """Return a summary dict used by the /status admin command."""
    today = date.today().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:

        # Pending posts (all time)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'pending'"
        )
        pending = (await cursor.fetchone())[0]

        # Published today
        cursor = await db.execute(
            """SELECT COUNT(*) FROM logs
               WHERE action = 'published' AND timestamp LIKE ?""",
            (f"{today}%",)
        )
        published_today = (await cursor.fetchone())[0]

        # Rejected today
        cursor = await db.execute(
            """SELECT COUNT(*) FROM logs
               WHERE action = 'rejected' AND timestamp LIKE ?""",
            (f"{today}%",)
        )
        rejected_today = (await cursor.fetchone())[0]

        # Submitted today (user only)
        cursor = await db.execute(
            """SELECT COUNT(*) FROM logs l
               JOIN posts p ON l.post_id = p.id
               WHERE l.action = 'submitted'
               AND p.source_type = 'user'
               AND l.timestamp LIKE ?""",
            (f"{today}%",)
        )
        user_submitted_today = (await cursor.fetchone())[0]

        # RSS fetched today
        cursor = await db.execute(
            """SELECT COUNT(*) FROM logs
               WHERE action = 'rss_fetched' AND timestamp LIKE ?""",
            (f"{today}%",)
        )
        rss_today = (await cursor.fetchone())[0]

        # Active RSS sources
        cursor = await db.execute(
            "SELECT COUNT(*) FROM sources WHERE active = 1"
        )
        active_sources = (await cursor.fetchone())[0]

        # Blocked users
        cursor = await db.execute("SELECT COUNT(*) FROM blocked_users")
        blocked_count = (await cursor.fetchone())[0]

    return {
        "pending":              pending,
        "published_today":      published_today,
        "rejected_today":       rejected_today,
        "user_submitted_today": user_submitted_today,
        "rss_today":            rss_today,
        "active_sources":       active_sources,
        "blocked_users":        blocked_count,
    }


async def get_pending_posts(limit: int = 10) -> list:
    """Return the oldest pending posts for the /pending command."""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, user_id, source_type, source_name, content_type, text
               FROM posts
               WHERE status = 'pending'
               ORDER BY id ASC
               LIMIT ?""",
            (limit,)
        )
        return await cursor.fetchall()


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