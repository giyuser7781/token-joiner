import os
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_API = "https://discord.com/api/v9"

SUPER_PROPERTIES = (
    "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xv"
    "Y2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3Mg"
    "TlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNr"
    "bykgQ2hyb21lLzEyNS4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI1"
    "LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWlu"
    "IjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIs"
    "InJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjMzNzYwNCwi"
    "Y2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
)


def clean_token(raw: str) -> str:
    t = raw.strip().replace("\r", "").replace("\n", "").replace("\t", "")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if t.lower().startswith("bot "):
        t = t[4:].strip()
    return t


def user_headers(token: str) -> dict:
    return {
        "Authorization": clean_token(token),
        "Content-Type": "application/json",
        "X-Super-Properties": SUPER_PROPERTIES,
        "X-Discord-Locale": "en-US",
        "X-Discord-Timezone": "America/New_York",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }


async def validate_token(session: aiohttp.ClientSession, token: str):
    """Returns (user_dict, error_string). One of them will be None."""
    try:
        async with session.get(
            f"{DISCORD_API}/users/@me", headers=user_headers(token)
        ) as resp:
            data = await resp.json()
            if resp.status == 401:
                return None, "Invalid or expired user token."
            if not resp.ok:
                return None, f"Token check failed — Discord returned {resp.status}."
            return data, None
    except Exception as e:
        return None, f"Network error during token validation: {e}"


async def check_guild_membership(session: aiohttp.ClientSession, token: str, guild_id: str):
    """Returns (True, None) if the token's user is in the guild, else (False, error)."""
    try:
        async with session.get(
            f"{DISCORD_API}/users/@me/guilds", headers=user_headers(token)
        ) as resp:
            if not resp.ok:
                return False, f"Could not fetch server list ({resp.status})."
            guilds = await resp.json()
            ids = {g["id"] for g in guilds}
            if guild_id not in ids:
                return False, "The user token is not a member of that server."
            return True, None
    except Exception as e:
        return False, f"Network error checking server membership: {e}"


async def check_channel(
    session: aiohttp.ClientSession, token: str, channel_id: str, guild_id: str
):
    """Returns (True, None) if channel exists in the guild and user can see it."""
    try:
        async with session.get(
            f"{DISCORD_API}/channels/{channel_id}", headers=user_headers(token)
        ) as resp:
            if resp.status == 403:
                return False, "The user has no permission to access that channel."
            if resp.status == 404:
                return False, "Channel not found — double-check the channel ID."
            if not resp.ok:
                return False, f"Channel check failed ({resp.status})."
            data = await resp.json()
            if str(data.get("guild_id", "")) != str(guild_id):
                return False, "That channel does not belong to the given server."
            channel_type = data.get("type", -1)
            if channel_type not in (0, 5):
                return False, "That channel is not a text channel."
            return True, None
    except Exception as e:
        return False, f"Network error checking channel: {e}"


async def send_one_message(
    session: aiohttp.ClientSession, token: str, channel_id: str, content: str
):
    """Send a single message. Handles 429 rate-limits automatically."""
    async with session.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=user_headers(token),
        json={"content": content},
    ) as resp:
        if resp.status == 429:
            data = await resp.json()
            retry_after = float(data.get("retry_after", 1))
            await asyncio.sleep(retry_after)
            return await send_one_message(session, token, channel_id, content)
        if not resp.ok:
            data = await resp.json()
            raise Exception(data.get("message", f"Send failed ({resp.status})."))


def interval_to_seconds(interval: int, unit: str) -> int:
    if unit == "h":
        return interval * 3600
    if unit == "m":
        return interval * 60
    return interval


class DispatchBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("[Dispatch] Slash commands synced globally.")

    async def on_ready(self):
        print(f"[Dispatch] Logged in as {self.user} (ID: {self.user.id})")


bot = DispatchBot()


@bot.tree.command(
    name="send",
    description="Send repeated messages to a channel using a Discord user token.",
)
@app_commands.describe(
    token="Your Discord user token (kept ephemeral — only you can see this command)",
    server_id="The ID of the server to send messages in",
    channel_id="The ID of the channel inside that server",
    message="The message text to send",
    count="How many times to send the message (e.g. 10)",
    interval="The gap between each message (numeric value)",
    unit="Time unit for the interval: s = seconds, m = minutes, h = hours",
)
@app_commands.choices(
    unit=[
        app_commands.Choice(name="Seconds (s)", value="s"),
        app_commands.Choice(name="Minutes (m)", value="m"),
        app_commands.Choice(name="Hours (h)", value="h"),
    ]
)
async def send_cmd(
    interaction: discord.Interaction,
    token: str,
    server_id: str,
    channel_id: str,
    message: str,
    count: int,
    interval: int,
    unit: str,
):
    # Ephemeral so only the caller sees the output — token never exposed publicly
    await interaction.response.defer(ephemeral=True)

    if count < 1:
        await interaction.edit_original_response(
            content="❌ **count** must be at least 1."
        )
        return
    if interval < 0:
        await interaction.edit_original_response(
            content="❌ **interval** cannot be negative."
        )
        return

    interval_secs = interval_to_seconds(interval, unit)
    unit_label = {"s": "second(s)", "m": "minute(s)", "h": "hour(s)"}[unit]

    async with aiohttp.ClientSession() as session:

        # ── Step 1: Validate token ────────────────────────────────────────────
        await interaction.edit_original_response(content="🔍 Validating token…")
        user, err = await validate_token(session, token)
        if err:
            await interaction.edit_original_response(content=f"❌ **Token Error:** {err}")
            return

        username = user.get("global_name") or user.get("username", "Unknown")

        # ── Step 2: Server membership ────────────────────────────────────────
        await interaction.edit_original_response(
            content=f"🔍 Checking if **{username}** is in server `{server_id}`…"
        )
        in_guild, err = await check_guild_membership(session, token, server_id)
        if err:
            await interaction.edit_original_response(
                content=f"❌ **Server Error:** {err}"
            )
            return

        # ── Step 3: Channel check ────────────────────────────────────────────
        await interaction.edit_original_response(
            content=f"🔍 Checking channel `{channel_id}`…"
        )
        ch_ok, err = await check_channel(session, token, channel_id, server_id)
        if err:
            await interaction.edit_original_response(
                content=f"❌ **Channel Error:** {err}"
            )
            return

        # ── Step 4: Send messages ────────────────────────────────────────────
        await interaction.edit_original_response(
            content=(
                f"✅ All checks passed.\n"
                f"👤 Sending as **{username}**\n"
                f"📡 Server `{server_id}` → Channel `{channel_id}`\n"
                f"📨 `{count}` message(s) · `{interval} {unit_label}` interval\n\n"
                f"⏳ `0 / {count}` sent…"
            )
        )

        sent = 0
        try:
            for i in range(1, count + 1):
                await send_one_message(session, token, channel_id, message)
                sent = i
                await interaction.edit_original_response(
                    content=(
                        f"📤 Sending as **{username}** → `{channel_id}`\n\n"
                        f"{'█' * i}{'░' * (count - i)} `{i} / {count}` sent"
                    )
                )
                if i < count:
                    await asyncio.sleep(interval_secs)

            await interaction.edit_original_response(
                content=(
                    f"✅ **Completed!**\n"
                    f"👤 Sent as **{username}**\n"
                    f"📡 Server `{server_id}` → Channel `{channel_id}`\n"
                    f"📨 `{count} / {count}` messages sent successfully."
                )
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=(
                    f"❌ **Error after {sent}/{count} messages sent.**\n"
                    f"Reason: {e}"
                )
            )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it in your Railway project settings."
        )
    bot.run(DISCORD_TOKEN)
