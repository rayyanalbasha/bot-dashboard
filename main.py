import os
import sys

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import shared_config as cfg

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_SECRET = os.getenv("SESSION_SECRET")

print("ENV CHECK:", {
    "DISCORD_CLIENT_ID": bool(CLIENT_ID),
    "DISCORD_CLIENT_SECRET": bool(CLIENT_SECRET),
    "DISCORD_REDIRECT_URI": bool(REDIRECT_URI),
    "BOT_TOKEN": bool(BOT_TOKEN),
    "SESSION_SECRET": bool(SESSION_SECRET),
}, file=sys.stderr)

missing = [name for name, val in [
    ("DISCORD_CLIENT_ID", CLIENT_ID),
    ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
    ("DISCORD_REDIRECT_URI", REDIRECT_URI),
    ("BOT_TOKEN", BOT_TOKEN),
    ("SESSION_SECRET", SESSION_SECRET),
] if not val]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

ADMINISTRATOR_PERMISSION = 0x8

cfg.init_db()


def _is_administrator(permissions: str | int) -> bool:
    try:
        return (int(permissions) & ADMINISTRATOR_PERMISSION) == ADMINISTRATOR_PERMISSION
    except (TypeError, ValueError):
        return False


@app.get("/")
async def home(request: Request):
    user = request.session.get("user")
    guilds = request.session.get("guilds", [])
    return templates.TemplateResponse(request, "index.html", {"user": user, "guilds": guilds, "config": {}})


@app.get("/login")
async def login():
    discord_login_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify%20guilds"
    )
    return RedirectResponse(discord_login_url)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


@app.get("/switch-account")
async def switch_account(request: Request):
    # Clears whoever is currently signed in, then sends them straight back
    # into Discord's OAuth flow so they can sign into a different account
    # without an extra click on "Login with Discord".
    request.session.clear()
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

        request.session["user"] = {"id": user_data.get("id"), "username": user_data.get("username")}
        request.session["guilds"] = filtered_guilds

        return templates.TemplateResponse(request, "index.html", {"user": user_data, "guilds": filtered_guilds, "config": {}})

    except Exception as e:
        return {"error_occurred": str(e)}


def _find_guild_in_session(request: Request, guild_id: str):
    guilds = request.session.get("guilds", [])
    for g in guilds:
        if str(g.get("id")) == str(guild_id):
            return g
    return None


@app.get("/dashboard/{guild_id}")
async def guild_dashboard(request: Request, guild_id: str):
    if not request.session.get("user"):
        return RedirectResponse(url="/")

    guild = _find_guild_in_session(request, guild_id)
    if guild is None or not _is_administrator(guild.get("permissions", 0)):
        guild_name = guild.get("name") if guild else None
        return templates.TemplateResponse(request, "access_denied.html", {"guild_name": guild_name})

    config = cfg.get_guild_config(guild_id)
    return templates.TemplateResponse(request, "guild.html", {"guild_id": guild_id, "config": config["punishments"] | {
        "welcome_msg": config["welcome_msg"],
        "bye_msg": config["bye_msg"],
    }})


def _require_admin(request: Request, guild_id: str):
    if not request.session.get("user"):
        return RedirectResponse(url="/")
    guild = _find_guild_in_session(request, guild_id)
    if guild is None or not _is_administrator(guild.get("permissions", 0)):
        return templates.TemplateResponse(
            None, "access_denied.html", {"guild_name": guild.get("name") if guild else None}
        )
    return None


@app.post("/dashboard/{guild_id}/update")
async def update_guild_dashboard(
    request: Request,
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
    denied = _require_admin(request, guild_id)
    if denied:
        return denied

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
async def add_bad_words(request: Request, guild_id: str, words: str = Form(...)):
    denied = _require_admin(request, guild_id)
    if denied:
        return denied
    added, skipped = cfg.add_bad_words(guild_id, words.split())
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/badwords/remove")
async def remove_bad_word(request: Request, guild_id: str, word: str = Form(...)):
    denied = _require_admin(request, guild_id)
    if denied:
        return denied
    cfg.remove_bad_word(guild_id, word)
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/log-channel")
async def set_log_channel(request: Request, guild_id: str, channel_id: str = Form(...)):
    denied = _require_admin(request, guild_id)
    if denied:
        return denied
    cfg.set_log_channel(guild_id, channel_id)
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)