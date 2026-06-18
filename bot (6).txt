import discord
from discord import app_commands
import aiohttp
import asyncio
import sqlite3
import threading
import uuid
import base64
import json
import os
import re
import random
from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN            = os.getenv("BOT_TOKEN")
OWNER_ID             = int(os.getenv("OWNER_ID", "0"))
WEBHOOK_URL          = os.getenv("WEBHOOK_URL")
NOPECHA_API_KEY      = os.getenv("NOPECHA_API_KEY", "")
DB_PATH              = os.getenv("DB_PATH", "accounts.db")
PORT                 = int(os.getenv("PORT", 8080))
_rpu                 = os.getenv("RAILWAY_PUBLIC_URL", "").strip().rstrip("/")
RAILWAY_URL          = _rpu if _rpu.startswith("http") else f"https://{_rpu}"

DISCORD_API      = "https://discord.com/api/v10"

# ─── Captcha session state ────────────────────────────────────────────────────

pending_captchas: dict[str, dict] = {}
bot_loop: asyncio.AbstractEventLoop | None = None

# ─── Database ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            token    TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            user_id  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def upsert_account(token: str, username: str, user_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO accounts (token, username, user_id) VALUES (?, ?, ?)",
        (token, username, user_id),
    )
    conn.commit()
    conn.close()


def get_all_accounts() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token, username, user_id FROM accounts")
    rows = c.fetchall()
    conn.close()
    return [{"token": r[0], "username": r[1], "user_id": r[2]} for r in rows]


# ─── Android header generator ─────────────────────────────────────────────────

ANDROID_VERSIONS  = ["13", "14"]
DISCORD_VERSIONS  = ["228.11 - rn", "229.4 - rn", "230.2 - rn"]
DISCORD_BUILDS    = list(range(228010, 230500, 100))
NATIVE_BUILDS     = list(range(100000, 110000, 50))
DEVICES = [
    ("Pixel 7",            "Google",   "pixel_7"),
    ("Pixel 7 Pro",        "Google",   "pixel_7_pro"),
    ("Pixel 8",            "Google",   "pixel_8"),
    ("SM-S918B",           "Samsung",  "samsung_s23_ultra"),
    ("SM-S911B",           "Samsung",  "samsung_s23"),
    ("IN2023",             "OnePlus",  "oneplus_9pro"),
    ("CPH2449",            "OPPO",     "oppo_reno8"),
    ("2201116PG",          "Xiaomi",   "xiaomi_12"),
]
LOCALES    = ["en-US", "en-GB", "en-CA"]
TIMEZONES  = ["America/New_York", "America/Los_Angeles", "Europe/London", "Asia/Tokyo"]


def make_headers(token: str | None = None) -> dict:
    device_name, vendor, device_slug = random.choice(DEVICES)
    os_ver      = random.choice(ANDROID_VERSIONS)
    disc_ver    = random.choice(DISCORD_VERSIONS)
    build_num   = random.choice(DISCORD_BUILDS)
    native_num  = random.choice(NATIVE_BUILDS)
    locale      = random.choice(LOCALES)
    timezone    = random.choice(TIMEZONES)
    device_id   = str(uuid.uuid4())

    super_props = {
        "os":                  "Android",
        "browser":             "Discord Android",
        "device":              device_name,
        "system_locale":       locale,
        "client_version":      disc_ver,
        "release_channel":     "stable",
        "device_vendor_id":    device_id,
        "browser_user_agent":  "",
        "browser_version":     "",
        "os_version":          os_ver,
        "client_build_number": build_num,
        "native_build_number": native_num,
        "client_event_source": None,
        "design_id":           0,
    }
    super_props_b64 = base64.b64encode(
        json.dumps(super_props, separators=(",", ":")).encode()
    ).decode()

    user_agent = (
        f"Discord-Android/{build_num}({build_num}) "
        f"Android/{os_ver} ({device_name}; {vendor.lower()}; {device_slug})"
    )

    headers = {
        "User-Agent":            user_agent,
        "X-Super-Properties":    super_props_b64,
        "X-Discord-Locale":      locale,
        "X-Discord-Timezone":    timezone,
        "Accept-Language":       f"{locale},{locale.split('-')[0]};q=0.9",
        "Content-Type":          "application/json",
        "Accept":                "*/*",
        "Connection":            "keep-alive",
    }
    if token:
        headers["Authorization"] = token
    return headers


# ─── Captcha helpers ──────────────────────────────────────────────────────────

def is_captcha_response(data: dict) -> bool:
    keys = data.get("captcha_key", [])
    return bool(keys)


async def notify_webhook(title: str, description: str, color: int = 0x5865F2) -> None:
    if not WEBHOOK_URL:
        return
    payload = {"embeds": [{"title": title, "description": description, "color": color}]}
    async with aiohttp.ClientSession() as s:
        await s.post(WEBHOOK_URL, json=payload)


def _captcha_page_url(session_id: str) -> str:
    """Return the direct URL to the hosted captcha page for this session."""
    return f"{RAILWAY_URL}/captcha/page/{session_id}"


async def fire_captcha_webhook(session_id: str, sitekey: str, rqdata: str, action_desc: str) -> None:
    """
    Send the owner a tap-to-open link to the hosted captcha page.
    The page loads hCaptcha pre-filled — user just taps the link and solves the puzzle.
    """
    if not WEBHOOK_URL:
        print(f"[CAPTCHA] No WEBHOOK_URL — session {session_id} pending, no alert sent")
        return

    page_url = _captcha_page_url(session_id)

    payload = {
        "embeds": [{
            "title": "🔐 Captcha Required",
            "description": (
                f"**Action:** {action_desc}\n\n"
                f"**[👉 Tap here to solve the captcha]({page_url})**\n\n"
                "1. Tap the link above\n"
                "2. Solve the puzzle in the page that opens\n"
                "3. Bot retries automatically ✅"
            ),
            "color": 0xFAA61A,
            "footer": {"text": "You have 5 minutes to solve this."},
        }]
    }
    async with aiohttp.ClientSession() as s:
        await s.post(WEBHOOK_URL, json=payload)


async def wait_for_captcha_solution(session_id: str, timeout: int = 300) -> str | None:
    entry = pending_captchas.get(session_id)
    if not entry:
        return None
    try:
        await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
        return entry.get("token")
    except asyncio.TimeoutError:
        return None
    finally:
        pending_captchas.pop(session_id, None)


async def solve_captcha_nopecha(sitekey: str, rqdata: str) -> str | None:
    if not NOPECHA_API_KEY:
        return None

    payload: dict = {
        "key":     NOPECHA_API_KEY,
        "type":    "hcaptcha",
        "sitekey": sitekey,
        "url":     "https://discord.com",
    }
    if rqdata:
        payload["data"] = rqdata

    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.nopecha.com/", json=payload) as resp:
            result = await resp.json(content_type=None)

        error_code = result.get("error")
        if error_code not in (None, 0, False):
            print(f"[NOPECHA] Submit failed (error {error_code}): {result}")
            return None

        task_id = result.get("id")
        if not task_id:
            print(f"[NOPECHA] No task ID in response: {result}")
            return None

        print(f"[NOPECHA] Task submitted: {task_id}")

        for attempt in range(60):
            await asyncio.sleep(5)
            async with s.get(
                "https://api.nopecha.com/",
                params={"key": NOPECHA_API_KEY, "id": task_id},
            ) as resp:
                result = await resp.json(content_type=None)

            poll_error = result.get("error")
            if poll_error == 9:
                continue

            if poll_error not in (None, 0, False):
                print(f"[NOPECHA] Error polling (error {poll_error}): {result}")
                return None

            data_list = result.get("data")
            if data_list and isinstance(data_list, list) and data_list[0]:
                print(f"[NOPECHA] Solved after {(attempt+1)*5}s")
                return data_list[0]

    print("[NOPECHA] Timed out waiting for solve")
    return None


# ─── Flask callback server ────────────────────────────────────────────────────

flask_app = Flask(__name__)


def _cors(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@flask_app.after_request
def after_request(response):
    return _cors(response)


@flask_app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


@flask_app.route("/captcha/solve", methods=["GET"])
def captcha_solve_get():
    """
    Receives the solved token via redirect from the javascript: snippet.
    window.location.href is used instead of fetch() to bypass Discord's CSP.
    """
    session_id = request.args.get("session_id", "").strip()
    token      = request.args.get("token", "").strip()

    if not session_id or not token:
        return (
            "<html><body style='background:#1e1e2e;color:#f38ba8;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<h2>❌ Missing session_id or token.</h2></body></html>"
        ), 400

    entry = pending_captchas.get(session_id)
    if not entry:
        return (
            "<html><body style='background:#1e1e2e;color:#f38ba8;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<h2>❌ Session expired or unknown. Run the command again.</h2></body></html>"
        ), 404

    print(f"[CAPTCHA] Token received via GET — session={session_id[:8]}… token_len={len(token)} first20={token[:20]}")

    entry["token"] = token
    if bot_loop and not bot_loop.is_closed():
        bot_loop.call_soon_threadsafe(entry["event"].set)

    return (
        "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body style='background:#1e1e2e;color:#cdd6f4;font-family:sans-serif;"
        "display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "height:100vh;margin:0;text-align:center;gap:12px;padding:20px'>"
        "<div style='font-size:56px'>✅</div>"
        "<h2 style='color:#a6e3a1;margin:0'>Captcha solved!</h2>"
        "<p style='color:#9ba4b4;margin:0'>The bot is now retrying the action.<br>"
        "You can close this tab.</p>"
        "</body></html>"
    ), 200, {"Content-Type": "text/html; charset=utf-8"}


@flask_app.route("/captcha/page/<session_id>", methods=["GET"])
def captcha_page(session_id: str):
    """
    Hosted captcha solving page. User taps the link from the webhook,
    solves the puzzle here, and the token is POSTed back automatically.
    No copy-paste needed.
    """
    entry = pending_captchas.get(session_id)
    if not entry:
        return (
            "<!DOCTYPE html><html><head>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Captcha — Expired</title></head>"
            "<body style='background:#1e1e2e;color:#f38ba8;font-family:sans-serif;"
            "display:flex;flex-direction:column;align-items:center;justify-content:center;"
            "height:100vh;margin:0;text-align:center;gap:12px;padding:20px'>"
            "<div style='font-size:56px'>⏰</div>"
            "<h2 style='color:#f38ba8;margin:0'>Session expired or already solved.</h2>"
            "<p style='color:#9ba4b4;margin:0'>Run the command again in Discord.</p>"
            "</body></html>"
        ), 404, {"Content-Type": "text/html; charset=utf-8"}

    sitekey = entry.get("sitekey", "")
    rqdata  = entry.get("rqdata",  "")

    # Build the JS snippet to copy (same redirect approach that works on discord.com)
    sid_js = json.dumps(session_id)
    sk_js  = json.dumps(sitekey)
    rd_js  = json.dumps(rqdata if rqdata else "")
    r_js   = json.dumps(RAILWAY_URL)

    js_snippet = (
        "javascript:(function(){"
        "if(document.getElementById('__cap_ov')){return;}"
        f"var sid={sid_js},sk={sk_js},rd={rd_js},R={r_js};"
        "var ov=document.createElement('div');"
        "ov.id='__cap_ov';"
        "ov.style.cssText='position:fixed;top:0;left:0;width:100%;height:100%;"
        "background:rgba(0,0,0,.93);z-index:2147483647;display:flex;"
        "flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:20px';"
        "ov.innerHTML='<p style=\"color:#cdd6f4;font-family:sans-serif;font-size:17px;margin:0\">"
        "Loading\u2026</p><div id=\"__cw\"></div>';"
        "document.body.appendChild(ov);"
        "function render(){"
        "try{hcaptcha.render('__cw',{sitekey:sk,rqdata:rd||undefined,theme:'dark',"
        "callback:function(t){"
        "ov.innerHTML='<p style=\"color:#a6e3a1;font-family:sans-serif;font-size:18px\">"
        "\u2705 Done! Sending\u2026</p>';"
        "location.href=R+'/captcha/solve?session_id='+sid+'&token='+encodeURIComponent(t);"
        "}});"
        "ov.querySelector('p').textContent='\u2B07\uFE0F Solve the captcha';"
        "}catch(e){ov.innerHTML='<p style=\"color:#f38ba8;font-family:sans-serif\">"
        "\u274C '+e.message+'</p>';}}"
        "var cb='__hc'+Math.random().toString(36).slice(2);"
        "window[cb]=render;"
        "var s=document.createElement('script');"
        "s.src='https://js.hcaptcha.com/1/api.js?render=explicit&onload='+cb;"
        "s.onerror=function(){ov.innerHTML='<p style=\"color:#f38ba8;font-family:sans-serif\">"
        "\u274C hcaptcha.js blocked</p>'};"
        "document.head.appendChild(s);"
        "})();"
    )

    snippet_json = json.dumps(js_snippet)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bot Captcha Helper</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #1e1e2e;
      color: #cdd6f4;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 24px;
      padding: 28px 20px;
      text-align: center;
    }}
    h2 {{ font-size: 22px; color: #cdd6f4; line-height: 1.3; }}
    .step {{
      background: #313244;
      border-radius: 12px;
      padding: 14px 18px;
      width: 100%;
      max-width: 420px;
      text-align: left;
      font-size: 15px;
      line-height: 1.6;
      color: #bac2de;
    }}
    .step span {{ color: #89b4fa; font-weight: 600; }}
    #copy-btn {{
      background: #5865f2;
      color: #fff;
      border: none;
      border-radius: 12px;
      padding: 18px 32px;
      font-size: 18px;
      font-weight: 700;
      cursor: pointer;
      width: 100%;
      max-width: 420px;
      letter-spacing: 0.3px;
      transition: background 0.15s;
    }}
    #copy-btn:active {{ background: #4752c4; }}
    #copy-btn.done {{ background: #40a02b; }}
    #msg {{
      font-size: 14px;
      color: #9ba4b4;
      min-height: 20px;
    }}
    #msg.ok  {{ color: #a6e3a1; }}
    #msg.err {{ color: #f38ba8; }}
  </style>
</head>
<body>
  <div style="font-size:52px">🔐</div>
  <h2>Bot needs a captcha solved</h2>

  <div class="step">
    <span>Step 1.</span> Tap the button below — it copies the snippet.<br>
    <span>Step 2.</span> Open Chrome → go to <b>discord.com</b><br>
    <span>Step 3.</span> Tap the address bar → paste → press Go<br>
    <span>Step 4.</span> Solve the puzzle that appears ✅
  </div>

  <button id="copy-btn" onclick="copySnippet()">📋 Copy Snippet</button>
  <p id="msg"></p>

  <script>
    var SNIPPET = {snippet_json};

    function copySnippet() {{
      var btn = document.getElementById('copy-btn');
      var msg = document.getElementById('msg');
      if (!navigator.clipboard) {{
        // Fallback: select a hidden textarea
        var ta = document.createElement('textarea');
        ta.value = SNIPPET;
        ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px';
        document.body.appendChild(ta);
        ta.select();
        try {{
          document.execCommand('copy');
          btn.textContent = '✅ Copied!';
          btn.className = 'done';
          msg.textContent = 'Now paste it in the Chrome address bar on discord.com';
          msg.className = 'ok';
        }} catch(e) {{
          msg.textContent = '❌ Could not copy automatically. Long-press the text below to copy.';
          msg.className = 'err';
          showFallback();
        }}
        document.body.removeChild(ta);
        return;
      }}
      navigator.clipboard.writeText(SNIPPET).then(function() {{
        btn.textContent = '✅ Copied!';
        btn.className = 'done';
        msg.textContent = 'Now paste it in the Chrome address bar on discord.com';
        msg.className = 'ok';
      }}).catch(function(e) {{
        msg.textContent = '❌ Copy failed: ' + e.message;
        msg.className = 'err';
        showFallback();
      }});
    }}

    function showFallback() {{
      var pre = document.createElement('textarea');
      pre.value = SNIPPET;
      pre.style.cssText = 'width:100%;max-width:420px;height:80px;background:#181825;color:#cdd6f4;'
        + 'border:1px solid #45475a;border-radius:8px;padding:10px;font-size:11px;resize:none';
      pre.readOnly = true;
      document.body.appendChild(pre);
      pre.select();
    }}
  </script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@flask_app.route("/captcha/solve", methods=["OPTIONS"])
def captcha_preflight():
    return jsonify({}), 200


@flask_app.route("/captcha/solve", methods=["POST"])
def captcha_solve_post():
    """Legacy POST endpoint — kept for compatibility."""
    data       = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "").strip()
    token      = data.get("token", "").strip()
    if not session_id or not token:
        return jsonify({"status": "error", "message": "session_id and token required"}), 400
    entry = pending_captchas.get(session_id)
    if not entry:
        return jsonify({"status": "error", "message": "Unknown or expired session"}), 404
    entry["token"] = token
    if bot_loop and not bot_loop.is_closed():
        bot_loop.call_soon_threadsafe(entry["event"].set)
    return jsonify({"status": "ok", "message": "Token received — bot is retrying…"})


@flask_app.route("/captcha/pending", methods=["GET", "OPTIONS"])
def captcha_pending():
    if not pending_captchas:
        return jsonify({"session_id": None}), 200
    session_id, entry = next(iter(pending_captchas.items()))
    return jsonify({
        "session_id": session_id,
        "sitekey":    entry.get("sitekey", ""),
        "rqdata":     entry.get("rqdata",  ""),
    })


def start_flask():
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)


# ─── Discord user API helpers ─────────────────────────────────────────────────

def extract_invite_code(invite_link: str) -> str:
    invite_link = invite_link.strip()
    match = re.search(
        r"discord(?:\.gg|app\.com/invite|\.com/invite)/([a-zA-Z0-9-]+)", invite_link
    )
    if match:
        return match.group(1)
    if re.match(r"^[a-zA-Z0-9-]+$", invite_link):
        return invite_link
    return invite_link


async def get_user_info(token: str) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{DISCORD_API}/users/@me", headers=make_headers(token)
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data


async def get_user_guilds(token: str) -> tuple[int, list]:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{DISCORD_API}/users/@me/guilds", headers=make_headers(token)
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = []
            if r.status == 200 and isinstance(data, list):
                return r.status, data
            return r.status, []


async def _prefetch_invite(token: str, invite_code: str) -> dict:
    params = "?with_counts=true&with_expiration=true"
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{DISCORD_API}/invites/{invite_code}{params}",
            headers=make_headers(token),
        ) as r:
            try:
                return await r.json()
            except Exception:
                return {}


async def _do_join(
    token: str,
    invite_code: str,
    captcha_token: str | None = None,
    captcha_rqtoken: str | None = None,
    invite_data: dict | None = None,
) -> tuple[int, dict]:
    headers = make_headers(token)

    guild   = (invite_data or {}).get("guild", {})
    channel = (invite_data or {}).get("channel", {})
    ctx_props = base64.b64encode(
        json.dumps({
            "location":              "Join Guild",
            "location_guild_id":     guild.get("id"),
            "location_channel_id":   channel.get("id"),
            "location_channel_type": channel.get("type"),
        }, separators=(",", ":")).encode()
    ).decode()
    headers["X-Context-Properties"] = ctx_props

    if captcha_token:
        headers["X-Captcha-Key"] = captcha_token
    if captcha_rqtoken:
        headers["X-Captcha-Rqtoken"] = captcha_rqtoken

    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{DISCORD_API}/invites/{invite_code}", headers=headers, json={}
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data


async def _do_leave(token: str, guild_id: str) -> int:
    async with aiohttp.ClientSession() as s:
        async with s.delete(
            f"{DISCORD_API}/users/@me/guilds/{guild_id}",
            headers=make_headers(token),
            json={"lurking": False},
        ) as r:
            return r.status


async def check_guild_membership(token: str, guild_id: str) -> bool:
    _, guilds = await get_user_guilds(token)
    return any(str(g.get("id")) == str(guild_id) for g in guilds)


# ─── Join with auto captcha solving ──────────────────────────────────────────

async def join_server_with_captcha(
    token: str,
    invite_code: str,
    action_desc: str,
) -> tuple[int, dict, str | None]:
    invite_data = await _prefetch_invite(token, invite_code)
    await asyncio.sleep(random.uniform(1.5, 4.0))
    status, data = await _do_join(token, invite_code, invite_data=invite_data)

    if status in (200, 204):
        return status, data, None

    if is_captcha_response(data):
        sitekey = data.get("captcha_sitekey", "")
        rqdata  = data.get("captcha_rqdata",  "")
        rqtoken = data.get("captcha_rqtoken", "")

        print(f"[CAPTCHA] Challenge received — keys in response: {list(data.keys())}")
        print(f"[CAPTCHA] sitekey   : {sitekey[:30] if sitekey else 'EMPTY'}")
        print(f"[CAPTCHA] rqdata_len: {len(rqdata) if rqdata else 0} chars {'(EMPTY — token will likely be rejected)' if not rqdata else ''}")
        print(f"[CAPTCHA] rqtoken   : {rqtoken[:30] if rqtoken else 'EMPTY'}")

        if NOPECHA_API_KEY:
            await notify_webhook(
                "🤖 Captcha Detected — Auto-solving…",
                f"**Action:** {action_desc}\n\nSubmitting to solving service — up to 60 s.",
                color=0xFAA61A,
            )
            solved_token = await solve_captcha_nopecha(sitekey, rqdata)
            if not solved_token:
                return 0, {}, "❌ Auto-captcha solver failed. Check your API key balance."
        else:
            session_id = str(uuid.uuid4())
            pending_captchas[session_id] = {
                "event":   asyncio.Event(),
                "token":   None,
                "sitekey": sitekey,
                "rqdata":  rqdata,
                "rqtoken": rqtoken,
            }
            await fire_captcha_webhook(session_id, sitekey, rqdata, action_desc)
            solved_token = await wait_for_captcha_solution(session_id)
            if not solved_token:
                return 0, {}, "⏰ **Captcha timed out** — you had 5 minutes. Run the command again."

        status, data = await _do_join(
            token, invite_code,
            captcha_token=solved_token, captcha_rqtoken=rqtoken,
            invite_data=invite_data,
        )
        print(f"[JOIN_RETRY] status={status} captcha_key={data.get('captcha_key')} code={data.get('code')} message={str(data.get('message',''))[:80]}")
        if status in (200, 204):
            return status, data, None
        if is_captcha_response(data):
            return 0, {}, (
                f"❌ **Captcha rejected by Discord.**\n"
                f"Status: `{status}` · Key: `{data.get('captcha_key')}`"
            )

    return status, data, parse_join_error(status, data)


# ─── Leave with captcha relay ─────────────────────────────────────────────────

async def leave_server_with_captcha(token: str, guild_id: str) -> tuple[int, str | None]:
    status = await _do_leave(token, guild_id)
    if status in (200, 204):
        return status, None
    if status == 401:
        return status, "❌ **Invalid token** — could not authenticate."
    if status == 404:
        return status, f"⚠️ Server `{guild_id}` not found or user is not in it."
    return status, f"❌ **Failed to leave** — Discord returned HTTP `{status}`."


# ─── Generic error parser ─────────────────────────────────────────────────────

def parse_join_error(status: int, data: dict) -> str:
    code    = data.get("code", 0)
    message = data.get("message", "")

    if status == 401:
        return "❌ **Invalid token** — the user token is incorrect or has been revoked."
    if status == 429:
        retry = data.get("retry_after", "?")
        return f"⏳ **Rate limited** — try again in `{retry}` seconds."
    if status == 400:
        if code == 10006:
            return "❌ **Invalid invite** — the invite link is expired or does not exist."
        if code == 40007:
            return "❌ **Banned** — this user is banned from that server."
        if code == 40002:
            return "🔒 **Verification required** — the server requires phone/email verification."
        if code == 50020:
            return "📛 **100 servers reached** — this account cannot join any more servers."
        if message:
            return f"❌ **Join failed** — {message} (code `{code}`)"
        return f"❌ **Join failed** — Discord returned 400 with no message. Raw: `{data}`"
    if status == 403:
        return f"❌ **Forbidden** — {message or 'no permission to join this server.'}"
    if status == 404:
        return "❌ **Invite not found** — double-check the invite link."
    return f"❌ **Unexpected error** — HTTP {status}: {message or 'unknown.'}"


# ─── Bot setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


def owner_only(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


async def deny(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🚫 Only the bot owner can use this command.", ephemeral=True
    )


# ─── /add ─────────────────────────────────────────────────────────────────────

@tree.command(name="add", description="Add a user account (token) to the bot's memory.")
@app_commands.describe(token="The Discord user token to add.")
async def cmd_add(interaction: discord.Interaction, token: str):
    if not owner_only(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    status, data = await get_user_info(token)
    if status != 200:
        return await interaction.followup.send(
            parse_join_error(status, data) or "❌ Could not fetch user info. Check the token.",
            ephemeral=True,
        )

    display_name = _display_name(data)
    user_id      = data.get("id", "")
    upsert_account(token, display_name, user_id)

    embed = discord.Embed(
        title="✅ Account Added",
        description=f"**{display_name}** (`{user_id}`) has been connected to the bot.",
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── /join ────────────────────────────────────────────────────────────────────

@tree.command(name="join", description="Make a user token join a server via invite link.")
@app_commands.describe(
    token="The Discord user token.",
    invite_link="Server invite link or code (e.g. discord.gg/abc123).",
)
async def cmd_join(interaction: discord.Interaction, token: str, invite_link: str):
    if not owner_only(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    status_user, user_data = await get_user_info(token)
    if status_user != 200:
        return await interaction.followup.send(
            parse_join_error(status_user, user_data) or "❌ Invalid or revoked user token.",
            ephemeral=True,
        )

    display_name = _display_name(user_data)
    user_id      = user_data.get("id", "")
    invite_code  = extract_invite_code(invite_link)
    action_desc  = f"`/join` — **{display_name}** → invite `{invite_code}`"

    final_status, final_data, err = await join_server_with_captcha(
        token, invite_code, action_desc
    )

    if err:
        return await interaction.followup.send(err, ephemeral=True)

    guild      = final_data.get("guild", {})
    guild_name = guild.get("name", "Unknown Server")
    guild_id   = guild.get("id", "?")

    upsert_account(token, display_name, user_id)

    embed = discord.Embed(title="✅ Joined Server", color=discord.Color.green())
    embed.add_field(name="Account", value=f"{display_name} (`{user_id}`)",   inline=False)
    embed.add_field(name="Server",  value=f"{guild_name} (`{guild_id}`)",    inline=False)
    embed.add_field(name="Invite",  value=f"`{invite_code}`",                inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── /stats ───────────────────────────────────────────────────────────────────

@tree.command(name="stats", description="Show all connected accounts and their live server counts.")
async def cmd_stats(interaction: discord.Interaction):
    if not owner_only(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    accounts = get_all_accounts()
    if not accounts:
        return await interaction.followup.send(
            "📭 No accounts connected yet. Use `/add`, `/join`, or `/profile` first.",
            ephemeral=True,
        )

    async def fetch_count(acc):
        _, guilds = await get_user_guilds(acc["token"])
        return acc["username"], len(guilds)

    results = await asyncio.gather(*[fetch_count(a) for a in accounts], return_exceptions=True)

    lines = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            lines.append(f"⚠️ **{accounts[i]['username']}** — error fetching guilds")
        else:
            uname, count = res
            lines.append(f"**{uname}** — {count} server{'s' if count != 1 else ''}")

    embed = discord.Embed(
        title=f"📊 Connected Accounts ({len(accounts)})",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── /profile ─────────────────────────────────────────────────────────────────

@tree.command(name="profile", description="View a user account's profile and server list by token.")
@app_commands.describe(token="The Discord user token.")
async def cmd_profile(interaction: discord.Interaction, token: str):
    if not owner_only(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    status_user, user_data = await get_user_info(token)
    if status_user != 200:
        return await interaction.followup.send(
            parse_join_error(status_user, user_data) or "❌ Invalid or revoked user token.",
            ephemeral=True,
        )

    display_name = _display_name(user_data)
    user_id      = user_data.get("id", "")

    status_g, guilds = await get_user_guilds(token)
    if status_g != 200:
        return await interaction.followup.send(
            "❌ Could not fetch server list for this token.", ephemeral=True
        )

    upsert_account(token, display_name, user_id)

    embed = discord.Embed(title=f"👤 {display_name}", color=discord.Color.og_blurple())
    embed.add_field(name="User ID",       value=f"`{user_id}`",   inline=True)
    embed.add_field(name="Total Servers", value=str(len(guilds)), inline=True)

    if guilds:
        lines = [
            f"• **{g.get('name', 'Unknown')}** — `{g.get('id', '?')}`"
            for g in guilds
        ]
        for chunk_start in range(0, len(lines), 20):
            chunk = lines[chunk_start:chunk_start + 20]
            embed.add_field(
                name=f"Servers {chunk_start + 1}–{chunk_start + len(chunk)}",
                value="\n".join(chunk),
                inline=False,
            )
    else:
        embed.add_field(name="Servers", value="Not in any servers.", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── /leave ───────────────────────────────────────────────────────────────────

@tree.command(name="leave", description="Make a user token leave a server by server ID.")
@app_commands.describe(
    token="The Discord user token.",
    server_id="The ID of the server to leave.",
)
async def cmd_leave(interaction: discord.Interaction, token: str, server_id: str):
    if not owner_only(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    status_user, user_data = await get_user_info(token)
    if status_user != 200:
        return await interaction.followup.send(
            parse_join_error(status_user, user_data) or "❌ Invalid or revoked user token.",
            ephemeral=True,
        )

    display_name = _display_name(user_data)
    user_id      = user_data.get("id", "")
    upsert_account(token, display_name, user_id)

    in_guild = await check_guild_membership(token, server_id)
    if not in_guild:
        return await interaction.followup.send(
            f"⚠️ **{display_name}** is not in a server with ID `{server_id}`.",
            ephemeral=True,
        )

    status_leave, err = await leave_server_with_captcha(token, server_id)
    if err:
        return await interaction.followup.send(err, ephemeral=True)

    embed = discord.Embed(title="✅ Left Server", color=discord.Color.red())
    embed.add_field(name="Account",   value=f"{display_name} (`{user_id}`)", inline=False)
    embed.add_field(name="Server ID", value=f"`{server_id}`",                inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── Utility ──────────────────────────────────────────────────────────────────

def _display_name(user_data: dict) -> str:
    username      = user_data.get("username", "unknown")
    discriminator = user_data.get("discriminator", "0")
    return f"{username}#{discriminator}" if discriminator not in ("0", None, "") else username


# ─── Bot events ───────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    global bot_loop
    bot_loop = asyncio.get_event_loop()
    init_db()
    await tree.sync()
    print(f"[BOT] Logged in as {client.user} (ID: {client.user.id})")
    print(f"[BOT] Owner ID   : {OWNER_ID}")
    print(f"[BOT] Flask port : {PORT}")
    print(f"[BOT] Railway URL: {RAILWAY_URL}")
    print(f"[BOT] NoPeCHA    : {'configured (auto-solve)' if NOPECHA_API_KEY else 'not set (tap-link captcha page)'}")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    print(f"[FLASK] Callback server started on port {PORT}")

    client.run(BOT_TOKEN)
