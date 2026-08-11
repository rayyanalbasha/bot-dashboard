import os
import sys

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import shared_config as cfg

load_dotenv()  # reads .env locally; no-op on Render (env vars are injected directly)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- TEMP DEBUG: remove once you've confirmed env vars are loading ---
# Prints True/False only — never the actual secret values — so it's safe
# to check in logs, but still remove this block once you're done debugging.
print("ENV CHECK:", {
    "DISCORD_CLIENT_ID": bool(CLIENT_ID),
    "DISCORD_CLIENT_SECRET": bool(CLIENT_SECRET),
    "DISCORD_REDIRECT_URI": bool(REDIRECT_URI),
    "BOT_TOKEN": bool(BOT_TOKEN),
}, file=sys.stderr)
# --- END TEMP DEBUG ---

missing = [name for name, val in [
    ("DISCORD_CLIENT_ID", CLIENT_ID),
    ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
    ("DISCORD_REDIRECT_URI", REDIRECT_URI),
    ("BOT_TOKEN", BOT_TOKEN),
] if not val]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

cfg.init_db()


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"user": None, "guilds": [], "config": {}})


@app.get("/login")
async def login():
    discord_login_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify%20guilds"
    )
    return RedirectResponse(discord_login_url)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str):
    try:
        token_url = "https://discord.com/api/oauth2/token"
        payload = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with httpx.AsyncClient() as client:
            response = await client.post(token_url, data=payload, headers=headers)
            token_data = response.json()

            if "access_token" not in token_data:
                return {"error": "Discord rejected token", "details": token_data}

            access_token = token_data.get("access_token")
            user_headers = {"Authorization": f"Bearer {access_token}"}

            user_response = await client.get("https://discord.com/api/users/@me", headers=user_headers)
            user_data = user_response.json()

            guilds_response = await client.get("https://discord.com/api/users/@me/guilds", headers=user_headers)
            user_guilds = guilds_response.json()

            bot_headers = {"Authorization": f"Bot {BOT_TOKEN}"}
            bot_guilds_response = await client.get("https://discord.com/api/users/@me/guilds", headers=bot_headers)

            bot_guilds = []
            if bot_guilds_response.status_code == 200:
                bot_guilds = bot_guilds_response.json()

            bot_guild_ids = {guild["id"] for guild in bot_guilds}
            filtered_guilds = [g for g in user_guilds if g["id"] in bot_guild_ids]

        return templates.TemplateResponse(request, "index.html", {"user": user_data, "guilds": filtered_guilds, "config": {}})

    except Exception as e:
        return {"error_occurred": str(e)}


@app.get("/dashboard/{guild_id}")
async def guild_dashboard(request: Request, guild_id: str):
    config = cfg.get_guild_config(guild_id)
    return templates.TemplateResponse(request, "guild.html", {"guild_id": guild_id, "config": config["punishments"] | {
        "welcome_msg": config["welcome_msg"],
        "bye_msg": config["bye_msg"],
    }})


@app.post("/dashboard/{guild_id}/update")
async def update_guild_dashboard(
    guild_id: str,
    bot_add: str = Form("ban"),
    member_kick: str = Form("ban"),
    member_ban: str = Form("ban"),
    server_change: str = Form("ban"),
    channels_edit: str = Form("ban"),
    emojis_edit: str = Form("ban"),
    roles_edit: str = Form("ban"),
    roles_remove: str = Form("ban"),
    welcome_msg: str = Form(""),
    bye_msg: str = Form(""),
    automod_filter: str = Form(""),
    automod_spam: str = Form("disabled"),
):
    # Map guild.html's field names -> the action keys bot.py/shared_config actually use
    updates = {
        "bot_add": bot_add,
        "member_kick": member_kick,
        "member_ban": member_ban,
        "server_change": server_change,
        "channel_change": channels_edit,
        "emoji_change": emojis_edit,
        "role_change": roles_edit,
        "unban": roles_remove,
    }
    for action, value in updates.items():
        cfg.set_punishment(guild_id, action, value)

    cfg.set_messages(guild_id, welcome_msg=welcome_msg, bye_msg=bye_msg)

    if automod_filter:
        cfg.add_bad_words(guild_id, automod_filter.split(","))

    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/badwords/add")
async def add_bad_words(guild_id: str, words: str = Form(...)):
    added, skipped = cfg.add_bad_words(guild_id, words.split())
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/badwords/remove")
async def remove_bad_word(guild_id: str, word: str = Form(...)):
    cfg.remove_bad_word(guild_id, word)
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/log-channel")
async def set_log_channel(guild_id: str, channel_id: str = Form(...)):
    cfg.set_log_channel(guild_id, channel_id)
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)