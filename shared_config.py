"""
shared_config.py

A tiny SQLite-backed config store shared by bot.py and dashboard.py.

Both processes import this module and call its functions. Because SQLite
just reads/writes a file on disk, this only works if both processes can
see the SAME file — i.e. they run on the same machine / same Render
service / same mounted volume.

If you later split the bot and dashboard into two separate Render
services, swap this for Postgres (e.g. via `asyncpg` or `psycopg`) — keep
the function signatures identical and nothing else has to change.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "config.db"

# Punishment actions the bot understands. Keep this list in sync with
# whatever action_type strings execute_punishment() is called with.
PUNISHMENT_ACTIONS = [
    "bot_add",
    "member_ban",
    "member_kick",
    "channel_change",
    "role_change",
    "emoji_change",
    "unban",
    "server_change",
]

DEFAULT_PUNISHMENTS = {action: "ban" for action in PUNISHMENT_ACTIONS}


def _default_row(guild_id: str) -> dict:
    return {
        "guild_id": guild_id,
        "punishments": dict(DEFAULT_PUNISHMENTS),
        "log_channel_id": None,
        "verification_role_id": None,
        "verification_channel_id": None,
        "bad_words": [],
        "welcome_msg": "",
        "bye_msg": "",
    }


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # lets reader/writer overlap safely
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
            """
        )


def get_guild_config(guild_id: int | str) -> dict:
    guild_id = str(guild_id)
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            data = _default_row(guild_id)
            conn.execute(
                "INSERT INTO guild_config (guild_id, data) VALUES (?, ?)",
                (guild_id, json.dumps(data)),
            )
            return data
        return json.loads(row[0])


def _save_guild_config(guild_id: str, data: dict):
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_config (guild_id, data) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET data = excluded.data
            """,
            (guild_id, json.dumps(data)),
        )


# ---------------------------------------------------------------- punishments

def get_punishment(guild_id: int | str, action: str) -> str:
    cfg = get_guild_config(guild_id)
    return cfg["punishments"].get(action, "ban")


def set_punishment(guild_id: int | str, action: str, value: str):
    """value: 'ban' | 'kick' | 'disabled'"""
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["punishments"][action] = value
    _save_guild_config(guild_id, cfg)


def get_all_punishments(guild_id: int | str) -> dict:
    return get_guild_config(guild_id)["punishments"]


# ---------------------------------------------------------------- log channel

def get_log_channel(guild_id: int | str):
    return get_guild_config(guild_id)["log_channel_id"]


def set_log_channel(guild_id: int | str, channel_id: int):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["log_channel_id"] = str(channel_id)
    _save_guild_config(guild_id, cfg)


# ---------------------------------------------------------------- verification

def get_verification(guild_id: int | str):
    cfg = get_guild_config(guild_id)
    if cfg["verification_role_id"] and cfg["verification_channel_id"]:
        return {
            "role_id": int(cfg["verification_role_id"]),
            "channel_id": int(cfg["verification_channel_id"]),
        }
    return None


def set_verification(guild_id: int | str, role_id: int, channel_id: int):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["verification_role_id"] = str(role_id)
    cfg["verification_channel_id"] = str(channel_id)
    _save_guild_config(guild_id, cfg)


def clear_verification(guild_id: int | str):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["verification_role_id"] = None
    cfg["verification_channel_id"] = None
    _save_guild_config(guild_id, cfg)


# ---------------------------------------------------------------- bad words

def get_bad_words(guild_id: int | str) -> list[str]:
    return get_guild_config(guild_id)["bad_words"]


def add_bad_words(guild_id: int | str, words: list[str]) -> tuple[int, int]:
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    existing = set(cfg["bad_words"])
    added = 0
    skipped = 0
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        if w in existing:
            skipped += 1
        else:
            cfg["bad_words"].append(w)
            existing.add(w)
            added += 1
    _save_guild_config(guild_id, cfg)
    return added, skipped


def remove_bad_word(guild_id: int | str, word: str) -> bool:
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    if word in cfg["bad_words"]:
        cfg["bad_words"].remove(word)
        _save_guild_config(guild_id, cfg)
        return True
    return False


# ---------------------------------------------------------------- welcome/bye

def set_messages(guild_id: int | str, welcome_msg: str = None, bye_msg: str = None):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    if welcome_msg is not None:
        cfg["welcome_msg"] = welcome_msg
    if bye_msg is not None:
        cfg["bye_msg"] = bye_msg
    _save_guild_config(guild_id, cfg)