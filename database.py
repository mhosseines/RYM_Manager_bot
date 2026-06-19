import aiosqlite

DB_NAME = "posts.db"


async def init_db():
    """Create the posts table if it doesn't exist."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content_type TEXT NOT NULL,   -- 'text', 'photo', or 'link'
                text TEXT,                    -- caption or text content
                file_id TEXT,                 -- telegram file_id for photo (if any)
                status TEXT DEFAULT 'pending' -- pending, approved, rejected, published
            )
        """)
        await db.commit()


async def add_post(user_id: int, content_type: str, text: str, file_id: str = None) -> int:
    """Insert a new post and return its ID."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO posts (user_id, content_type, text, file_id, status) VALUES (?, ?, ?, ?, 'pending')",
            (user_id, content_type, text, file_id)
        )
        await db.commit()
        return cursor.lastrowid


async def get_post(post_id: int):
    """Fetch a single post by ID. Returns a tuple or None."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return await cursor.fetchone()


async def update_status(post_id: int, status: str):
    """Update the status of a post (pending, approved, rejected, published)."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
        await db.commit()
