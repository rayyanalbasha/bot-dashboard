"""
shared_config.py
...
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "config.db"
print(f"[shared_config] DB_PATH resolved to: {DB_PATH.resolve()}", flush=True)
print(f"[shared_config] DB file exists: {DB_PATH.exists()}", flush=True)

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
    conn.execute("PRAGMA journal_mode=WAL")
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
            print(f"[shared_config] get_guild_config: NO ROW for guild_id={guild_id!r} -> created default (all 'ban')", flush=True)
            return data
        result = json.loads(row[0])
        print(f"[shared_config] get_guild_config: guild_id={guild_id!r} punishments={result.get('punishments')}", flush=True)
        return result


def _save_guild_config(guild_id: str, data: dict):
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_config (guild_id, data) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET data = excluded.data
            """,
            (guild_id, json.dumps(data)),
        )
    print(f"[shared_config] _save_guild_config: WROTE guild_id={guild_id!r} punishments={data.get('punishments')}", flush=True)


def get_punishment(guild_id: int | str, action: str) -> str:
    cfg = get_guild_config(guild_id)
    return cfg["punishments"].get(action, "ban")


def set_punishment(guild_id: int | str, action: str, value: str):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["punishments"][action] = value
    _save_guild_config(guild_id, cfg)


def get_all_punishments(guild_id: int | str) -> dict:
    return get_guild_config(guild_id)["punishments"]


def get_log_channel(guild_id: int | str):
    return get_guild_config(guild_id)["log_channel_id"]


def set_log_channel(guild_id: int | str, channel_id: int):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["log_channel_id"] = str(channel_id)
    _save_guild_config(guild_id, cfg)


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


def set_messages(guild_id: int | str, welcome_msg: str = None, bye_msg: str = None):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    if welcome_msg is not None:
        cfg["welcome_msg"] = welcome_msg
    if bye_msg is not None:
        cfg["bye_msg"] = bye_msg
    _save_guild_config(guild_id, cfg)