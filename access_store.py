"""Persistent Telegram access grants and one-use username invitations."""

import re
import sqlite3


def normalize_username(value: str) -> str | None:
    value = value.removeprefix("@")
    return value.lower() if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", value) else None


class AccessStore:
    def __init__(self, path: str, initial_ids: frozenset[int]):
        self.db = sqlite3.connect(path)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY)")
            self.db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS invitations (username TEXT PRIMARY KEY)")
            if not self.db.execute("SELECT 1 FROM settings WHERE key = 'initialized'").fetchone():
                self.db.executemany("INSERT INTO users(id) VALUES (?)", ((i,) for i in initial_ids))
                self.db.execute("INSERT INTO settings VALUES ('initialized')")

    def contains(self, user_id: int) -> bool:
        return bool(self.db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone())

    def authorize(self, user_id: int, username: str | None) -> bool:
        username = normalize_username(username) if username else None
        with self.db:
            allowed = self.contains(user_id)
            if not allowed and username:
                claimed = self.db.execute(
                    "DELETE FROM invitations WHERE username = ? RETURNING username", (username,)
                ).fetchone()
                if claimed:
                    self.db.execute("INSERT INTO users(id) VALUES (?)", (user_id,))
                    allowed = True
            if allowed:
                if username:
                    # Usernames can be reassigned; only the latest observed owner keeps it.
                    self.db.execute("UPDATE users SET username = NULL WHERE username = ? AND id != ?", (username, user_id))
                    self.db.execute("DELETE FROM invitations WHERE username = ?", (username,))
                self.db.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
            return allowed

    def invite(self, username: str) -> bool:
        with self.db:
            if self.db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                return False
            self.db.execute("INSERT OR IGNORE INTO invitations VALUES (?)", (username,))
            return True

    def remove(self, value: str) -> tuple[list[int], bool]:
        with self.db:
            if value.isascii() and value.isdigit():
                ids = [int(value)] if self.contains(int(value)) else []
                self.db.execute("DELETE FROM users WHERE id = ?", (int(value),))
                return ids, bool(ids)
            username = normalize_username(value)
            if not username:
                return [], False
            ids = [row[0] for row in self.db.execute("SELECT id FROM users WHERE username = ?", (username,))]
            self.db.execute("DELETE FROM users WHERE username = ?", (username,))
            pending = self.db.execute("DELETE FROM invitations WHERE username = ?", (username,)).rowcount
            return ids, bool(ids or pending)

    def remove_all(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM users")
            self.db.execute("DELETE FROM invitations")

    def list_users(self) -> tuple[list[tuple[int, str | None]], list[str]]:
        return (
            self.db.execute("SELECT id, username FROM users ORDER BY id").fetchall(),
            [row[0] for row in self.db.execute("SELECT username FROM invitations ORDER BY username")],
        )

    def close(self) -> None:
        self.db.close()
