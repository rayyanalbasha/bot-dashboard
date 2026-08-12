"""
shared_config.py

Postgres-backed config store shared by sec.py (running on Wispbyte) and
main.py (running on Render, or wherever). Both processes talk to the SAME
hosted database over the network via DATABASE_URL, so a change made
through either deployment's panel is visible to both immediately -- no
more "two separate local config.db files" problem.
"""

import json
import os
from contextlib import contextmanager

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

print(f"[shared_config] Using Postgres DATABASE_URL (host hidden): {'set' if DATABASE_URL else 'MISSING'}", flush=True)

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

# Actions (punishments + standalone protections) that can be granted to a
# member via the /القائمه-البيضاء (whitelist) panel so they bypass them.
BYPASSABLE_ACTIONS = PUNISHMENT_ACTIONS + ["everyone_mention"]


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
        # Bans any member whose Discord account is younger than 48 hours
        # the moment they join. "enabled" / "disabled" only.
        "account_age_protection": "disabled",
        # Times out members who mention @everyone/@here.
        # "enabled" / "disabled" only.
        "everyone_mention_protection": "disabled",
        # user_id (str) -> list of action keys (from BYPASSABLE_ACTIONS)
        # that this user is exempt from.
        "bypass_permissions": {},
    }


@contextmanager
def _conn():
    conn = psycopg.connect(DATABASE_URL)
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
                data JSONB NOT NULL
            )
            """
        )


def get_guild_config(guild_id: int | str) -> dict:
    guild_id = str(guild_id)
    with _conn() as conn:
        row = conn.execute(
            "SELECT data FROM guild_config WHERE guild_id = %s", (guild_id,)
        ).fetchone()
        if row is None:
            data = _default_row(guild_id)
            conn.execute(
                "INSERT INTO guild_config (guild_id, data) VALUES (%s, %s)",
                (guild_id, json.dumps(data)),
            )
            print(f"[shared_config] get_guild_config: NO ROW for guild_id={guild_id!r} -> created default (all 'ban')", flush=True)
            return data
        result = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    # Backfill any keys that older rows (created before this feature was
    # added) might be missing, so callers can always rely on .get() working
    # without every single call site needing a fallback.
    defaults = _default_row(guild_id)
    missing = False
    for key, val in defaults.items():
        if key not in result:
            result[key] = val
            missing = True
    if missing:
        _save_guild_config(guild_id, result)

    print(f"[shared_config] get_guild_config: guild_id={guild_id!r} punishments={result.get('punishments')}", flush=True)
    return result


def _save_guild_config(guild_id: str, data: dict):
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_config (guild_id, data) VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET data = EXCLUDED.data
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


# ==================== New Account-Age Join Protection ====================

def get_account_age_protection(guild_id: int | str) -> str:
    return get_guild_config(guild_id).get("account_age_protection", "disabled")


def set_account_age_protection(guild_id: int | str, value: str):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["account_age_protection"] = "enabled" if value == "enabled" else "disabled"
    _save_guild_config(guild_id, cfg)


# ==================== @everyone / @here Mention Protection ====================

def get_everyone_protection(guild_id: int | str) -> str:
    return get_guild_config(guild_id).get("everyone_mention_protection", "disabled")


def set_everyone_protection(guild_id: int | str, value: str):
    guild_id = str(guild_id)
    cfg = get_guild_config(guild_id)
    cfg["everyone_mention_protection"] = "enabled" if value == "enabled" else "disabled"
    _save_guild_config(guild_id, cfg)


# ==================== Bypass Permissions (القائمة البيضاء) ====================

def get_bypass_permissions(guild_id: int | str) -> dict:
    """Returns the full { user_id_str: [action, ...] } mapping for a guild."""
    return get_guild_config(guild_id).get("bypass_permissions", {})


def get_user_bypass(guild_id: int | str, user_id: int | str) -> list[str]:
    return get_bypass_permissions(guild_id).get(str(user_id), [])


def is_bypassed(guild_id: int | str, user_id: int | str, action: str) -> bool:
    return action in get_user_bypass(guild_id, user_id)


def add_bypass_permission(guild_id: int | str, user_id: int | str, action: str):
    guild_id = str(guild_id)
    user_id = str(user_id)
    cfg = get_guild_config(guild_id)
    bypass = cfg.setdefault("bypass_permissions", {})
    actions = bypass.setdefault(user_id, [])
    if action not in actions:
        actions.append(action)
    _save_guild_config(guild_id, cfg)


def remove_bypass_user(guild_id: int | str, user_id: int | str) -> bool:
    """Removes ALL bypass permissions for a single user (the دإزالة تصريح action)."""
    guild_id = str(guild_id)
    user_id = str(user_id)
    cfg = get_guild_config(guild_id)
    bypass = cfg.setdefault("bypass_permissions", {})
    if user_id in bypass:
        del bypass[user_id]
        _save_guild_config(guild_id, cfg)
        return True
    return False