import os

import httpx
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import shared_config as cfg

app = FastAPI()
templates = Jinja2Templates(directory="templates")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "1534993557067399328")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")  # set this in your environment, do not hardcode
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "https://bot-dashboard-l46h.onrender.com/auth/callback")
BOT_TOKEN = os.getenv("BOT_TOKEN")

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
    return templates.TemplateResponse(request, "guild.html", {"guild_id": guild_id, "config": config})


# NOTE: these field names now match the action keys the bot actually checks
# (cfg.PUNISHMENT_ACTIONS = bot_add, member_ban, member_kick, channel_change,
# role_change, emoji_change, unban, server_change). Update your guild.html
# form's `name="..."` attributes to match these exactly, or the values won't
# bind. Each value should be "ban", "kick", or "disabled".
@app.post("/dashboard/{guild_id}/update")
async def update_guild_dashboard(
    guild_id: str,
    bot_add: str = Form("ban"),
    member_ban: str = Form("ban"),
    member_kick: str = Form("ban"),
    channel_change: str = Form("ban"),
    role_change: str = Form("ban"),
    emoji_change: str = Form("ban"),
    unban: str = Form("ban"),
    server_change: str = Form("ban"),
    welcome_msg: str = Form(""),
    bye_msg: str = Form(""),
):
    updates = {
        "bot_add": bot_add,
        "member_ban": member_ban,
        "member_kick": member_kick,
        "channel_change": channel_change,
        "role_change": role_change,
        "emoji_change": emoji_change,
        "unban": unban,
        "server_change": server_change,
    }
    for action, value in updates.items():
        cfg.set_punishment(guild_id, action, value)

    cfg.set_messages(guild_id, welcome_msg=welcome_msg, bye_msg=bye_msg)

    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)


@app.post("/dashboard/{guild_id}/badwords/add")
async def add_bad_words(guild_id: str, words: str = Form(...)):
    """words: space or newline separated list, matching the /فلتر_لوحة modal behavior."""
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