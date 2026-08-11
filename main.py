from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
import traceback
import os
import json

app = FastAPI()

templates = Jinja2Templates(directory="templates")

CLIENT_ID = "1534993557067399328"
CLIENT_SECRET = "QB8seNuOLAfJH4I9M-mFjlFtSoGmRTez"
REDIRECT_URI = "https://bot-dashboard-l46h.onrender.com/auth/callback"
BOT_TOKEN = os.getenv("BOT_TOKEN")

CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

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
        return {"error_occurred": str(e), "trace": traceback.format_exc()}

@app.get("/dashboard/{guild_id}")
async def guild_dashboard(request: Request, guild_id: str):
    config = load_config()
    return templates.TemplateResponse(request, "guild.html", {"guild_id": guild_id, "config": config})

@app.post("/dashboard/{guild_id}/update")
async def update_guild_dashboard(
    guild_id: str,
    bot_add: str = Form("disabled"),
    member_kick: str = Form("disabled"),
    member_ban: str = Form("disabled"),
    server_change: str = Form("disabled"),
    channels_edit: str = Form("disabled"),
    emojis_edit: str = Form("disabled"),
    roles_edit: str = Form("disabled"),
    roles_remove: str = Form("disabled"),
    welcome_msg: str = Form(""),
    bye_msg: str = Form(""),
    automod_filter: str = Form(""),
    automod_spam: str = Form("disabled")
):
    config = {
        "bot_add": bot_add,
        "member_kick": member_kick,
        "member_ban": member_ban,
        "server_change": server_change,
        "channels_edit": channels_edit,
        "emojis_edit": emojis_edit,
        "roles_edit": roles_edit,
        "roles_remove": roles_remove,
        "welcome_msg": welcome_msg,
        "bye_msg": bye_msg,
        "automod_filter": automod_filter,
        "automod_spam": automod_spam
    }
    save_config(config)
    return RedirectResponse(url=f"/dashboard/{guild_id}", status_code=303)