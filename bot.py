import hmac
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from aiohttp import web
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
HWID_COOLDOWN = 24 * 60 * 60  # seconds between HWID resets
BRAND = "BananaHub"
# Admins are never punished for leaks unless you set EXEMPT_ADMINS=false (for testing)
EXEMPT_ADMINS = os.getenv("EXEMPT_ADMINS", "true").lower() != "false"
# Secret for the Windows admin app (min. 16 chars). Without it the API stays disabled.
API_KEY = os.getenv("ADMIN_API_KEY", "")

DURATIONS = {
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
    "lifetime": None,
}

START_TIME = time.time()


# ----------------------------- storage ---------------------------------
def load() -> dict:
    d = {}
    if DATA_FILE.exists():
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for k in ("panels", "keys", "users", "banned", "config", "appeals"):
        d.setdefault(k, {})
    return d


def save(d: dict) -> None:
    DATA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


data = load()


def get_license(user_id: int, panel: str):
    return data["users"].get(str(user_id), {}).get(panel)


def license_error(user_id: int, panel: str):
    """Returns an error text if the user may NOT use the panel, else None."""
    if str(user_id) in data["banned"]:
        return "🚫 You are banned from using this service."
    lic = get_license(user_id, panel)
    if not lic:
        return "🔒 You need to **Redeem Key** first!"
    if lic.get("deleted"):
        return "🗑️ Your Key got Successfully Deleted. Please buy a New Key!"
    entry = data["keys"].get(lic["key"])
    if entry is None or entry.get("banned"):
        return "🚫 Your key has been banned."
    exp = lic.get("expires_at")
    if exp and exp < time.time():
        return "⌛ Your key has expired."
    return None


def fmt_duration(d: str) -> str:
    return d.capitalize()


def panels_online() -> bool:
    return data["config"].get("panels_online", True)


async def apply_presence():
    """Show the panel status in the bot's Discord presence."""
    try:
        if panels_online():
            await bot.change_presence(status=discord.Status.online, activity=None)
        elif data["config"].get("invisible_when_offline"):
            await bot.change_presence(status=discord.Status.invisible, activity=None)
        else:
            await bot.change_presence(
                status=discord.Status.dnd,
                activity=discord.Game(name="Panels offline"),
            )
    except Exception:
        pass  # not connected yet


async def strip_roles(user_id: int, only_panel: str | None = None):
    """Remove the panel role(s) from a user on every guild the bot is in."""
    for pname, panel in data["panels"].items():
        if only_panel and pname != only_panel:
            continue
        role_id = panel.get("role_id")
        for guild in bot.guilds:
            role = guild.get_role(role_id) if role_id else None
            if not role:
                continue
            try:
                member = guild.get_member(user_id) or await guild.fetch_member(user_id)
                if role in member.roles:
                    await member.remove_roles(role, reason="Key expired / banned")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


def ban_user_keys(user_id: int, reason: str):
    data["banned"][str(user_id)] = {"reason": reason, "at": int(time.time())}
    for lic in data["users"].get(str(user_id), {}).values():
        entry = data["keys"].get(lic["key"])
        if entry:
            entry["banned"] = True
    for entry in data["keys"].values():
        if entry.get("locked_to") == user_id:
            entry["banned"] = True
    save(data)


def lift_user_ban(user_id: int):
    data["banned"].pop(str(user_id), None)
    for lic in data["users"].get(str(user_id), {}).values():
        entry = data["keys"].get(lic["key"])
        if entry:
            entry["banned"] = False
    for entry in data["keys"].values():
        if entry.get("locked_to") == user_id:
            entry["banned"] = False
    save(data)


# ----------------------------- logging ---------------------------------
async def log_event(title: str, description: str, color: discord.Color | None = None):
    cid = data["config"].get("log_channel")
    if not cid:
        return
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=BRAND)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ----------------------------- panel embed -----------------------------
def build_panel_embed(name: str) -> discord.Embed:
    panel = data["panels"][name]
    embed = discord.Embed(title=name, description=panel["description"], color=discord.Color.blurple())
    embed.set_footer(text=f"{BRAND} • Redeem your key first to unlock the other buttons.")
    return embed


# ----------------------------- redeem modal ----------------------------
class RedeemModal(discord.ui.Modal, title="Redeem Key"):
    key_input = discord.ui.TextInput(
        label="Your key",
        placeholder="XXXX-XXXX-XXXX-XXXX",
        min_length=5,
        max_length=64,
    )

    def __init__(self, panel: str):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        key = self.key_input.value.strip()
        entry = data["keys"].get(key)

        if str(uid) in data["banned"]:
            return await interaction.response.send_message(
                "🚫 You are banned from using this service.", ephemeral=True
            )
        if entry is None or entry["panel"] != self.panel:
            return await interaction.response.send_message(
                "❌ Invalid key for this panel.", ephemeral=True
            )
        if entry.get("banned"):
            return await interaction.response.send_message(
                "🚫 This key has been banned.", ephemeral=True
            )
        if entry.get("locked_to") and entry["locked_to"] != uid:
            return await interaction.response.send_message(
                "❌ This key belongs to another user.", ephemeral=True
            )
        if entry["used_by"] is not None:
            return await interaction.response.send_message(
                "❌ This key has already been redeemed.", ephemeral=True
            )
        existing = get_license(uid, self.panel)
        if existing:
            ex_exp = existing.get("expires_at")
            replaceable = existing.get("deleted") or (ex_exp and ex_exp < time.time())
            if not replaceable:
                return await interaction.response.send_message(
                    "⚠️ You already have a key redeemed on this panel.", ephemeral=True
                )

        duration = entry.get("duration", "lifetime")
        seconds = DURATIONS.get(duration)
        now = int(time.time())
        expires_at = now + seconds if seconds else None

        entry["used_by"] = uid
        data["users"].setdefault(str(uid), {})[self.panel] = {
            "key": key,
            "duration": duration,
            "hwid_reset_at": 0,
            "redeemed_at": now,
            "expires_at": expires_at,
            "token": "BH-" + secrets.token_hex(8),  # unique leak-watermark
        }
        save(data)

        exp_text = f"expires <t:{expires_at}:R>" if expires_at else "never expires"
        await interaction.response.send_message(
            f"✅ **{fmt_duration(duration)}** key redeemed — {exp_text}.\n"
            "You can now use **Get Script**, **Get Role** and **Reset HWID**.",
            ephemeral=True,
        )
        await log_event(
            "🔑 Key redeemed",
            f"{interaction.user.mention} (`{uid}`) redeemed a **{fmt_duration(duration)}** key on **{self.panel}**.",
            discord.Color.green(),
        )


# ----------------------------- panel view ------------------------------
class PanelView(discord.ui.View):
    def __init__(self, panel: str):
        super().__init__(timeout=None)  # persistent
        self.panel = panel

        self.add_item(self._btn("Redeem Key", discord.ButtonStyle.success, "🔑", "redeem", self.redeem))
        self.add_item(self._btn("Get Script", discord.ButtonStyle.primary, "📄", "script", self.script))
        self.add_item(self._btn("Get Role", discord.ButtonStyle.secondary, "🎭", "role", self.role))
        self.add_item(self._btn("Reset HWID", discord.ButtonStyle.danger, "🔄", "hwid", self.hwid))
        self.add_item(self._btn("Get Stats", discord.ButtonStyle.secondary, "📊", "stats", self.stats))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """While the panels are offline only 'Get Stats' works."""
        if panels_online():
            return True
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.endswith(":stats"):
            return True
        await interaction.response.send_message(
            "🔴 The panel is currently **offline**. Only **Get Stats** is available right now.",
            ephemeral=True,
        )
        return False

    def _btn(self, label, style, emoji, cid, callback):
        b = discord.ui.Button(
            label=label, style=style, emoji=emoji, custom_id=f"panel:{self.panel}:{cid}"
        )
        b.callback = callback
        return b

    async def _require_key(self, interaction: discord.Interaction) -> bool:
        err = license_error(interaction.user.id, self.panel)
        if err is None:
            return True
        await interaction.response.send_message(err, ephemeral=True)
        return False

    async def redeem(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RedeemModal(self.panel))

    async def script(self, interaction: discord.Interaction):
        if not await self._require_key(interaction):
            return
        lic = get_license(interaction.user.id, self.panel)
        if not lic.get("token"):  # licenses redeemed before the leak-protection update
            lic["token"] = "BH-" + secrets.token_hex(8)
            save(data)
        script = data["panels"][self.panel]["script"]
        # personal watermark: if this token ever shows up in a channel, the owner is banned
        marked = f"-- {lic['token']}\n{script}"
        await interaction.response.send_message(
            f"📄 **Your personal script** — do NOT share it, it is tied to you. "
            f"Leaking = permanent ban.\n```lua\n{marked}\n```",
            ephemeral=True,
        )

    async def role(self, interaction: discord.Interaction):
        if not await self._require_key(interaction):
            return
        role_id = data["panels"][self.panel].get("role_id")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role is None:
            return await interaction.response.send_message(
                "❌ No role configured for this panel.", ephemeral=True
            )
        if role in interaction.user.roles:
            return await interaction.response.send_message(
                "⚠️ You already have this role.", ephemeral=True
            )
        try:
            await interaction.user.add_roles(role, reason="Key redeemed")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I can't assign that role (move my role above it).", ephemeral=True
            )
        await interaction.response.send_message(
            f"✅ You received the role {role.mention}!", ephemeral=True
        )

    async def hwid(self, interaction: discord.Interaction):
        if not await self._require_key(interaction):
            return
        lic = get_license(interaction.user.id, self.panel)
        remaining = lic["hwid_reset_at"] + HWID_COOLDOWN - time.time()
        if remaining > 0:
            return await interaction.response.send_message(
                f"⏳ HWID reset on cooldown. Try again <t:{int(time.time() + remaining)}:R>.",
                ephemeral=True,
            )
        lic["hwid_reset_at"] = int(time.time())
        lic["hwid"] = None  # your script/API re-binds on next launch
        save(data)
        await interaction.response.send_message("✅ Your HWID has been reset.", ephemeral=True)
        await log_event(
            "🔄 HWID reset",
            f"{interaction.user.mention} (`{interaction.user.id}`) reset their HWID on **{self.panel}**.",
            discord.Color.orange(),
        )

    async def stats(self, interaction: discord.Interaction):
        b: commands.Bot = interaction.client
        total_keys = len(data["keys"])
        used = sum(1 for k in data["keys"].values() if k["used_by"] is not None)
        banned = sum(1 for k in data["keys"].values() if k.get("banned"))
        uptime = int(time.time() - START_TIME)
        d, r = divmod(uptime, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)

        embed = discord.Embed(title=f"📊 {BRAND} Stats", color=discord.Color.blurple())
        embed.add_field(
            name="Panel status",
            value="🟢 Online" if panels_online() else "🔴 Offline",
            inline=True,
        )
        embed.add_field(name="Latency", value=f"{round(b.latency * 1000)} ms", inline=True)
        embed.add_field(name="Uptime", value=f"{d}d {h}h {m}m {s}s", inline=True)
        embed.add_field(name="Servers", value=str(len(b.guilds)), inline=True)
        embed.add_field(name="Panels", value=str(len(data["panels"])), inline=True)
        embed.add_field(name="Keys", value=f"{used}/{total_keys} redeemed", inline=True)
        embed.add_field(name="Banned keys", value=str(banned), inline=True)

        lic = get_license(interaction.user.id, self.panel)
        if lic and not lic.get("deleted"):
            exp = lic.get("expires_at")
            embed.add_field(
                name="Your key",
                value=f"{fmt_duration(lic.get('duration', 'lifetime'))} — "
                + (f"expires <t:{exp}:R>" if exp else "lifetime"),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------------------- appeal system ---------------------------
class AppealModal(discord.ui.Modal, title="Unban Appeal"):
    message = discord.ui.TextInput(
        label="Why should we unban you?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. It was an accident, I did not know...",
        min_length=5,
        max_length=1000,
    )

    def __init__(self, uid: int, gid: int):
        super().__init__()
        self.uid = uid
        self.gid = gid

    async def on_submit(self, interaction: discord.Interaction):
        if not data["config"].get("log_channel"):
            return await interaction.response.send_message(
                "❌ Appeals are currently unavailable. Please contact the staff directly.",
                ephemeral=True,
            )
        data["appeals"][str(self.uid)] = {
            "status": "pending",
            "text": self.message.value,
            "guild_id": self.gid,
            "at": int(time.time()),
        }
        save(data)

        embed = discord.Embed(
            title="📨 New Unban Appeal",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"<@{self.uid}> (`{self.uid}`)", inline=False)
        embed.add_field(
            name="Ban reason",
            value=data["banned"].get(str(self.uid), {}).get("reason", "Unknown"),
            inline=False,
        )
        embed.add_field(name="Appeal", value=self.message.value, inline=False)
        embed.set_footer(text=BRAND)

        view = discord.ui.View(timeout=None)
        view.add_item(DecisionButton("ok", self.uid, self.gid))
        view.add_item(DecisionButton("no", self.uid, self.gid))

        channel = bot.get_channel(data["config"]["log_channel"])
        if channel is None:
            channel = await bot.fetch_channel(data["config"]["log_channel"])
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Your appeal was sent to the staff. You will get a DM with the decision.",
            ephemeral=True,
        )


class AppealButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"appeal:(?P<uid>[0-9]+):(?P<gid>[0-9]+)",
):
    def __init__(self, uid: int, gid: int):
        super().__init__(
            discord.ui.Button(
                label="Unban Appeal",
                style=discord.ButtonStyle.primary,
                emoji="📨",
                custom_id=f"appeal:{uid}:{gid}",
            )
        )
        self.uid = uid
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["uid"]), int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message("❌ This appeal is not yours.", ephemeral=True)
        existing = data["appeals"].get(str(self.uid))
        if existing and existing["status"] == "pending":
            return await interaction.response.send_message(
                "⏳ You already have a pending appeal. Please wait for the staff.", ephemeral=True
            )
        if existing and existing["status"] == "denied":
            return await interaction.response.send_message(
                "🚫 Your appeal was already denied.", ephemeral=True
            )
        await interaction.response.send_modal(AppealModal(self.uid, self.gid))


class DecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"appeal_(?P<act>ok|no):(?P<uid>[0-9]+):(?P<gid>[0-9]+)",
):
    def __init__(self, act: str, uid: int, gid: int):
        accept = act == "ok"
        super().__init__(
            discord.ui.Button(
                label="Accept & Unban" if accept else "Deny",
                style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                emoji="✅" if accept else "❌",
                custom_id=f"appeal_{act}:{uid}:{gid}",
            )
        )
        self.act = act
        self.uid = uid
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["act"], int(match["uid"]), int(match["gid"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        appeal = data["appeals"].setdefault(str(self.uid), {})
        embed = interaction.message.embeds[0]
        try:
            user = await interaction.client.fetch_user(self.uid)
        except discord.HTTPException:
            user = None

        if self.act == "ok":
            guild = interaction.client.get_guild(self.gid) or interaction.guild
            try:
                await guild.unban(discord.Object(id=self.uid), reason=f"{BRAND}: appeal accepted")
            except discord.NotFound:
                pass  # was not banned on the server
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ I can't unban (missing Ban Members permission).", ephemeral=True
                )
            lift_user_ban(self.uid)
            appeal["status"] = "accepted"
            save(data)
            embed.color = discord.Color.green()
            embed.add_field(name="Decision", value=f"✅ Accepted by {interaction.user.mention}", inline=False)

            invite_text = ""
            try:
                invite = await interaction.channel.create_invite(max_uses=1, max_age=86400, unique=True)
                invite_text = f"\nRejoin here (valid 24h): {invite.url}"
            except (discord.Forbidden, discord.HTTPException):
                pass
            if user:
                try:
                    await user.send(f"✅ Your unban appeal was **accepted**. You were unbanned.{invite_text}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        else:
            appeal["status"] = "denied"
            save(data)
            embed.color = discord.Color.red()
            embed.add_field(name="Decision", value=f"❌ Denied by {interaction.user.mention}", inline=False)
            if user:
                try:
                    await user.send("❌ Your unban appeal was **denied**.")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        await interaction.response.edit_message(embed=embed, view=None)


class OrphanPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"panel:(?P<name>.+):(?P<act>redeem|script|role|hwid|stats)",
):
    """Catches clicks on panel messages whose panel is not registered (e.g. data was reset)."""

    def __init__(self, name: str, act: str):
        super().__init__(discord.ui.Button(label=act, custom_id=f"panel:{name}:{act}"))
        self.name = name
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["name"], match["act"])

    async def callback(self, interaction: discord.Interaction):
        if self.name in data["panels"]:
            view = PanelView(self.name)
            if await view.interaction_check(interaction):
                await getattr(view, self.act)(interaction)
            return
        await interaction.response.send_message(
            f"⚠️ The panel **{self.name}** is not active anymore (deleted or the bot data was reset).\n"
            "An admin only needs to run `/setuppanel` again with the **same name** — this message then works again.",
            ephemeral=True,
        )


async def dm_banned(user_id: int, guild: discord.Guild, reason: str) -> bool:
    """DM the user BEFORE the ban (afterwards the bot may no longer share a server with them)."""
    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="🔨 You have been banned",
            description=(
                f"You got banned from **{guild.name}**.\n\n"
                f"**Reason:** {reason}\n\n"
                "If you think this was a mistake, please open an appeal with the button below."
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text=BRAND)
        view = discord.ui.View(timeout=None)
        view.add_item(AppealButton(user_id, guild.id))
        await user.send(embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ----------------------------- bot -------------------------------------
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for leak detection (enable in Dev Portal!)
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_dynamic_items(AppealButton, DecisionButton, OrphanPanelButton)
        for name in data["panels"]:
            self.add_view(PanelView(name))
        expiry_loop.start()
        await start_api()
        await self.tree.sync()
        print(
            f"Loaded {len(data['panels'])} panels, {len(data['keys'])} keys, "
            f"{len(data['users'])} users from {DATA_FILE.resolve()}"
        )


bot = Bot()


@tasks.loop(minutes=5)
async def expiry_loop():
    now = time.time()
    changed = False
    for uid, panels in data["users"].items():
        for pname, lic in panels.items():
            exp = lic.get("expires_at")
            if exp and exp < now and not lic.get("role_removed"):
                await strip_roles(int(uid), only_panel=pname)
                lic["role_removed"] = True
                changed = True
    if changed:
        save(data)


# ----------------------------- leak detection --------------------------
async def collect_text(message: discord.Message) -> str:
    """Gather every piece of text a message carries (content, forwards, embeds, text files)."""
    parts = [message.content or ""]

    def add_embeds(embeds):
        for e in embeds:
            parts.append(e.title or "")
            parts.append(e.description or "")
            for f in e.fields:
                parts.append(f.name or "")
                parts.append(f.value or "")

    add_embeds(message.embeds)
    attachments = list(message.attachments)

    # Discord's "Forward" feature stores the text in snapshots
    for snap in getattr(message, "message_snapshots", None) or []:
        parts.append(getattr(snap, "content", "") or "")
        add_embeds(getattr(snap, "embeds", []) or [])
        attachments.extend(getattr(snap, "attachments", []) or [])

    for att in attachments:
        name = (att.filename or "").lower()
        if att.size <= 500_000 and name.endswith((".txt", ".lua", ".luau", ".md", ".json")):
            try:
                parts.append((await att.read()).decode("utf-8", errors="ignore"))
            except (discord.HTTPException, discord.NotFound):
                pass
    return "\n".join(parts)


async def check_leak(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    content = await collect_text(message)
    if not content.strip():
        return

    for uid, panels in data["users"].items():
        for lic in panels.values():
            token = lic.get("token")
            if not token or token not in content:
                continue

            owner_id = int(uid)
            member = message.guild.get_member(owner_id)
            if EXEMPT_ADMINS and member and member.guild_permissions.administrator:
                try:
                    await message.channel.send(
                        f"⚠️ Leak detected from <@{owner_id}>, but admins are exempt "
                        f"(test with a non-admin account or set EXEMPT_ADMINS=false)."
                    )
                except discord.HTTPException:
                    pass
                return

            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

            reason = "You shared the script publicly (sent it to someone else)."
            ban_user_keys(owner_id, f"Leaked script (found in #{message.channel})")
            await strip_roles(owner_id)

            dm_ok = await dm_banned(owner_id, message.guild, reason)  # DM first, then ban
            try:
                await message.guild.ban(
                    discord.Object(id=owner_id),
                    reason=f"{BRAND}: script leak detected",
                    delete_message_days=0,
                )
                outcome = "banned from the server"
            except (discord.Forbidden, discord.HTTPException) as e:
                outcome = (
                    f"key-banned, but I could not ban the user ({type(e).__name__}: "
                    "give me Ban Members and put my role above theirs)"
                )
            try:
                await message.channel.send(
                    f"🚨 A leaked script was detected. <@{owner_id}> was {outcome}."
                )
            except discord.HTTPException:
                pass
            await log_event(
                "🚨 Script leak",
                f"<@{owner_id}> (`{owner_id}`) leaked the script in {message.channel.mention}.\n"
                f"Result: {outcome}.\nDM sent: {'yes' if dm_ok else 'no (DMs closed)'}",
                discord.Color.red(),
            )
            return


@bot.event
async def on_message(message: discord.Message):
    await check_leak(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    await check_leak(after)


# ----------------------------- commands --------------------------------
async def panel_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in data["panels"]
        if current.lower() in p.lower()
    ][:25]


@bot.tree.command(name="setuppanel", description="Create/configure a panel (then post it with /sendpanel)")
@app_commands.describe(
    name="Unique panel name",
    description="Text shown on the panel",
    script="The script content users receive (e.g. loadstring line)",
    role="Role given by the Get Role button",
)
@app_commands.default_permissions(administrator=True)
async def setup_panel(
    interaction: discord.Interaction,
    name: str,
    description: str,
    script: str,
    role: discord.Role,
):
    if name in data["panels"]:
        return await interaction.response.send_message(
            "❌ A panel with that name already exists. Use `/deletepanel` first.",
            ephemeral=True,
        )
    data["panels"][name] = {"description": description, "script": script, "role_id": role.id}
    save(data)
    bot.add_view(PanelView(name))
    await interaction.response.send_message(
        f"✅ Panel **{name}** saved. Post it with `/sendpanel panel:{name}`.", ephemeral=True
    )
    await log_event("🧩 Panel created", f"{interaction.user.mention} created panel **{name}**.")


@bot.tree.command(name="sendpanel", description="Post a saved panel into a channel")
@app_commands.describe(panel="The panel you set up", channel="Where to post it (default: this channel)")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def send_panel(
    interaction: discord.Interaction,
    panel: str,
    channel: discord.TextChannel | None = None,
):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)
    channel = channel or interaction.channel
    try:
        await channel.send(embed=build_panel_embed(panel), view=PanelView(panel))
    except discord.Forbidden:
        return await interaction.response.send_message(
            f"❌ I can't send messages in {channel.mention}. Give me **View Channel**, "
            "**Send Messages** and **Embed Links** there.",
            ephemeral=True,
        )
    await interaction.response.send_message(
        f"✅ Panel **{panel}** posted in {channel.mention}.", ephemeral=True
    )
    await log_event("📤 Panel sent", f"{interaction.user.mention} posted **{panel}** in {channel.mention}.")


@bot.tree.command(name="deletepanel", description="Delete a panel and its keys")
@app_commands.describe(panel="Panel to delete")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def delete_panel(interaction: discord.Interaction, panel: str):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)
    del data["panels"][panel]
    data["keys"] = {k: v for k, v in data["keys"].items() if v["panel"] != panel}
    for user_panels in data["users"].values():
        user_panels.pop(panel, None)
    save(data)
    await interaction.response.send_message(
        f"🗑️ Panel **{panel}** deleted. You can delete the old panel message manually.",
        ephemeral=True,
    )
    await log_event("🗑️ Panel deleted", f"{interaction.user.mention} deleted panel **{panel}**.", discord.Color.orange())


@bot.tree.command(name="setlogchannel", description="Set the channel for logs and unban appeals")
@app_commands.describe(channel="The log channel")
@app_commands.default_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    try:
        await channel.send(embed=discord.Embed(
            title="✅ Log channel set",
            description="Logs and unban appeals will appear here.",
            color=discord.Color.green(),
        ))
    except discord.Forbidden:
        return await interaction.response.send_message(
            f"❌ I can't send messages in {channel.mention}.", ephemeral=True
        )
    data["config"]["log_channel"] = channel.id
    save(data)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="createkey", description="Generate keys for a panel")
@app_commands.describe(
    panel="Which panel the key is for",
    duration="Daily / Weekly / Monthly / Lifetime",
    user="Optional: lock the key to this user (nobody else can redeem it)",
    amount="How many keys (1-25)",
)
@app_commands.choices(
    duration=[
        app_commands.Choice(name="Daily (1 day)", value="daily"),
        app_commands.Choice(name="Weekly (7 days)", value="weekly"),
        app_commands.Choice(name="Monthly (30 days)", value="monthly"),
        app_commands.Choice(name="Lifetime", value="lifetime"),
    ]
)
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def create_key(
    interaction: discord.Interaction,
    panel: str,
    duration: app_commands.Choice[str],
    user: discord.User | None = None,
    amount: app_commands.Range[int, 1, 25] = 1,
):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)

    keys = []
    for _ in range(amount):
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        data["keys"][key] = {
            "panel": panel,
            "used_by": None,
            "duration": duration.value,
            "locked_to": user.id if user else None,
            "banned": False,
            "created_at": int(time.time()),
        }
        keys.append(key)
    save(data)

    target = f" — locked to {user.mention}" if user else ""
    await interaction.response.send_message(
        f"🔑 **{amount} {duration.name} key(s) for {panel}**{target}\n```\n"
        + "\n".join(keys)
        + "\n```",
        ephemeral=True,
    )
    await log_event(
        "🔑 Keys created",
        f"{interaction.user.mention} created **{amount}x {duration.name}** key(s) for **{panel}**"
        + (f", locked to {user.mention}." if user else "."),
    )


def key_status(entry: dict) -> str:
    if entry.get("banned"):
        return "🚫 Banned"
    if entry.get("deleted"):
        return "❌ Deleted"
    if entry["used_by"] is not None:
        return "✅ Redeemed"
    return "🟢 Unused"


@bot.tree.command(name="keyinfo", description="Show info about a key or a user")
@app_commands.describe(key="The key to look up", user="Or: the user to look up")
@app_commands.default_permissions(administrator=True)
async def key_info(
    interaction: discord.Interaction,
    key: str | None = None,
    user: discord.User | None = None,
):
    if not key and not user:
        return await interaction.response.send_message(
            "❌ Provide a `key` or a `user`.", ephemeral=True
        )

    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_footer(text=BRAND)

    if key:
        key = key.strip()
        entry = data["keys"].get(key)
        if not entry:
            return await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        embed.title = "🔎 Key info"
        embed.add_field(name="Key", value=f"`{key}`", inline=False)
        embed.add_field(name="Panel", value=entry["panel"], inline=True)
        embed.add_field(name="Type", value=fmt_duration(entry.get("duration", "lifetime")), inline=True)
        embed.add_field(name="Status", value=key_status(entry), inline=True)
        embed.add_field(
            name="Locked to",
            value=f"<@{entry['locked_to']}>" if entry.get("locked_to") else "Anyone",
            inline=True,
        )
        if entry["used_by"] is not None:
            embed.add_field(name="Redeemed by", value=f"<@{entry['used_by']}>", inline=True)
            lic = get_license(entry["used_by"], entry["panel"])
            if lic and lic.get("key") == key:
                exp = lic.get("expires_at")
                embed.add_field(
                    name="Expires",
                    value=f"<t:{exp}:R>" if exp else "Never (lifetime)",
                    inline=True,
                )
        if entry.get("created_at"):
            embed.add_field(name="Created", value=f"<t:{entry['created_at']}:R>", inline=True)
    else:
        embed.title = f"🔎 User info — {user}"
        ban = data["banned"].get(str(user.id))
        embed.add_field(
            name="Banned",
            value=f"🚫 Yes — {ban['reason']}" if ban else "No",
            inline=False,
        )
        licenses = data["users"].get(str(user.id), {})
        if not licenses:
            embed.add_field(name="Licenses", value="None", inline=False)
        for pname, lic in licenses.items():
            exp = lic.get("expires_at")
            embed.add_field(
                name=pname,
                value=(
                    f"Key: `{lic['key']}`\n"
                    f"Type: {fmt_duration(lic.get('duration', 'lifetime'))}\n"
                    f"Expires: {f'<t:{exp}:R>' if exp else 'Never'}"
                    + ("\nStatus: ❌ Deleted" if lic.get("deleted") else "")
                ),
                inline=False,
            )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="listkeys", description="List the keys of a panel")
@app_commands.describe(panel="Which panel", status="Which keys to show")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Unused", value="unused"),
        app_commands.Choice(name="Redeemed", value="used"),
        app_commands.Choice(name="Banned", value="banned"),
        app_commands.Choice(name="All", value="all"),
    ]
)
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def list_keys(
    interaction: discord.Interaction,
    panel: str,
    status: app_commands.Choice[str] | None = None,
):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)
    mode = status.value if status else "unused"

    lines = []
    for key, entry in data["keys"].items():
        if entry["panel"] != panel:
            continue
        if mode == "unused" and (entry["used_by"] is not None or entry.get("banned")):
            continue
        if mode == "used" and entry["used_by"] is None:
            continue
        if mode == "banned" and not entry.get("banned"):
            continue
        owner = entry.get("used_by") or entry.get("locked_to")
        lines.append(
            f"{key} | {fmt_duration(entry.get('duration', 'lifetime')):<8} | "
            f"{key_status(entry)[2:]:<8} | {owner if owner else '-'}"
        )

    if not lines:
        return await interaction.response.send_message("ℹ️ No keys found.", ephemeral=True)

    header = f"{panel} — {mode} keys ({len(lines)})\nKEY | TYPE | STATUS | USER-ID\n"
    text = header + "\n".join(lines)
    if len(text) < 1800:
        await interaction.response.send_message(f"```\n{text}\n```", ephemeral=True)
    else:
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename=f"{panel}_{mode}_keys.txt")
        await interaction.response.send_message(
            f"📄 {len(lines)} keys (too long for a message):", file=file, ephemeral=True
        )


@bot.tree.command(name="bankey", description="Ban a user's keys (and optionally the user)")
@app_commands.describe(
    user="The Discord user to ban",
    reason="Reason (optional)",
    ban_from_server="Also ban the user from this server (they get a DM with an appeal button)",
)
@app_commands.default_permissions(administrator=True)
async def ban_key(
    interaction: discord.Interaction,
    user: discord.User,
    reason: str = "No reason given",
    ban_from_server: bool = False,
):
    await interaction.response.defer(ephemeral=True)
    ban_user_keys(user.id, reason)
    await strip_roles(user.id)

    extra = ""
    if ban_from_server and interaction.guild:
        await dm_banned(user.id, interaction.guild, reason)  # DM first, then ban
        try:
            await interaction.guild.ban(user, reason=f"{BRAND}: {reason}", delete_message_days=0)
            extra = " and banned from the server"
        except (discord.Forbidden, discord.HTTPException):
            extra = " (could not ban from server — missing permission)"
    await interaction.followup.send(
        f"🔨 {user.mention}'s keys were banned{extra}.\nReason: {reason}", ephemeral=True
    )
    await log_event(
        "🔨 Key ban",
        f"{interaction.user.mention} banned {user.mention} (`{user.id}`){extra}.\nReason: {reason}",
        discord.Color.red(),
    )


@bot.tree.command(name="unbankey", description="Lift a key ban from a user")
@app_commands.describe(user="The Discord user to unban")
@app_commands.default_permissions(administrator=True)
async def unban_key(interaction: discord.Interaction, user: discord.User):
    lift_user_ban(user.id)
    await interaction.response.send_message(
        f"✅ Key ban lifted for {user.mention}. (Server bans must be lifted in Server Settings → Bans.)",
        ephemeral=True,
    )
    await log_event(
        "✅ Key unban",
        f"{interaction.user.mention} lifted the key ban of {user.mention} (`{user.id}`).",
        discord.Color.green(),
    )


@bot.tree.command(name="deletekey", description="Delete a user's key on a panel")
@app_commands.describe(user="The user whose key should be deleted", panel="The panel")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def delete_key(interaction: discord.Interaction, user: discord.User, panel: str):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)
    lic = get_license(user.id, panel)
    if not lic or lic.get("deleted"):
        return await interaction.response.send_message(
            f"❌ {user.mention} has no active key on **{panel}**.", ephemeral=True
        )
    lic["deleted"] = True
    lic["deleted_at"] = int(time.time())
    entry = data["keys"].get(lic["key"])
    if entry:
        entry["deleted"] = True
    save(data)
    await strip_roles(user.id, only_panel=panel)
    await interaction.response.send_message(
        f"🗑️ Deleted {user.mention}'s key on **{panel}**. Buttons now show: "
        "*Your Key got Successfully Deleted. Please buy a New Key!*",
        ephemeral=True,
    )
    await log_event(
        "🗑️ Key deleted",
        f"{interaction.user.mention} deleted the key of {user.mention} (`{user.id}`) on **{panel}**.",
        discord.Color.orange(),
    )


@bot.tree.command(name="extendkey", description="Extend a user's key by a number of days")
@app_commands.describe(user="The key owner", panel="The panel", days="Days to add")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def extend_key(
    interaction: discord.Interaction,
    user: discord.User,
    panel: str,
    days: app_commands.Range[int, 1, 3650],
):
    lic = get_license(user.id, panel)
    if not lic or lic.get("deleted"):
        return await interaction.response.send_message(
            f"❌ {user.mention} has no active key on **{panel}**.", ephemeral=True
        )
    if not lic.get("expires_at"):
        return await interaction.response.send_message(
            "ℹ️ That is a Lifetime key — nothing to extend.", ephemeral=True
        )
    lic["expires_at"] = int(max(lic["expires_at"], time.time()) + days * 86400)
    lic["role_removed"] = False
    save(data)
    await interaction.response.send_message(
        f"⏩ {user.mention}'s key on **{panel}** extended by **{days}** day(s). "
        f"New expiry: <t:{lic['expires_at']}:F>",
        ephemeral=True,
    )
    await log_event(
        "⏩ Key extended",
        f"{interaction.user.mention} extended {user.mention}'s key on **{panel}** by {days} day(s).",
    )


@bot.tree.command(name="resethwid", description="Reset a user's HWID (ignores the cooldown)")
@app_commands.describe(user="The key owner", panel="The panel")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def admin_reset_hwid(interaction: discord.Interaction, user: discord.User, panel: str):
    lic = get_license(user.id, panel)
    if not lic or lic.get("deleted"):
        return await interaction.response.send_message(
            f"❌ {user.mention} has no active key on **{panel}**.", ephemeral=True
        )
    lic["hwid"] = None
    lic["hwid_reset_at"] = 0
    save(data)
    await interaction.response.send_message(
        f"🔄 HWID of {user.mention} on **{panel}** was reset.", ephemeral=True
    )
    await log_event(
        "🔄 HWID reset (admin)",
        f"{interaction.user.mention} reset the HWID of {user.mention} on **{panel}**.",
        discord.Color.orange(),
    )


@bot.tree.command(name="panels", description="Show all panels with their key numbers")
@app_commands.default_permissions(administrator=True)
async def list_panels(interaction: discord.Interaction):
    if not data["panels"]:
        return await interaction.response.send_message(
            "ℹ️ No panels yet. Create one with `/setuppanel`.", ephemeral=True
        )
    embed = discord.Embed(title="🧩 Panels", color=discord.Color.blurple())
    embed.set_footer(text=BRAND)
    for name, panel in data["panels"].items():
        keys = [k for k in data["keys"].values() if k["panel"] == name]
        unused = sum(1 for k in keys if k["used_by"] is None and not k.get("banned"))
        redeemed = sum(1 for k in keys if k["used_by"] is not None and not k.get("deleted"))
        banned = sum(1 for k in keys if k.get("banned"))
        deleted = sum(1 for k in keys if k.get("deleted"))
        role = f"<@&{panel['role_id']}>" if panel.get("role_id") else "—"
        embed.add_field(
            name=name,
            value=(
                f"Role: {role}\nKeys: **{len(keys)}** "
                f"(🟢 {unused} unused • ✅ {redeemed} redeemed • 🚫 {banned} banned • ❌ {deleted} deleted)"
            ),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="adminstats", description="Detailed statistics about keys and users")
@app_commands.default_permissions(administrator=True)
async def admin_stats(interaction: discord.Interaction):
    now = time.time()
    active = {t: 0 for t in DURATIONS}
    expiring = 0
    redeemed_week = 0
    for panels in data["users"].values():
        for lic in panels.values():
            if lic.get("redeemed_at", 0) > now - 7 * 86400:
                redeemed_week += 1
            if lic.get("deleted"):
                continue
            exp = lic.get("expires_at")
            if exp and exp < now:
                continue
            active[lic.get("duration", "lifetime")] = active.get(lic.get("duration", "lifetime"), 0) + 1
            if exp and exp - now < 86400:
                expiring += 1
    pending = sum(1 for a in data["appeals"].values() if a.get("status") == "pending")

    embed = discord.Embed(title=f"📈 {BRAND} — Admin stats", color=discord.Color.blurple())
    embed.add_field(
        name="Active licenses",
        value="\n".join(f"{fmt_duration(t)}: **{n}**" for t, n in active.items()),
        inline=True,
    )
    embed.add_field(
        name="Overview",
        value=(
            f"Panels: **{len(data['panels'])}**\n"
            f"Keys total: **{len(data['keys'])}**\n"
            f"Redeemed (7 days): **{redeemed_week}**\n"
            f"Expiring in 24h: **{expiring}**\n"
            f"Banned users: **{len(data['banned'])}**\n"
            f"Pending appeals: **{pending}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Panel status",
        value="🟢 Online" if panels_online() else "🔴 Offline",
        inline=False,
    )
    embed.set_footer(text=BRAND)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="backup", description="Download a backup of all bot data (keys, users, panels)")
@app_commands.default_permissions(administrator=True)
async def backup(interaction: discord.Interaction):
    payload = json.dumps(data, indent=2).encode("utf-8")
    file = discord.File(io.BytesIO(payload), filename=f"{BRAND.lower()}_backup_{int(time.time())}.json")
    await interaction.response.send_message(
        "💾 Backup created. Keep this file private — it contains every key.",
        file=file,
        ephemeral=True,
    )


@bot.tree.command(name="restore", description="Restore bot data from a backup file")
@app_commands.describe(file="A backup .json created with /backup")
@app_commands.default_permissions(administrator=True)
async def restore(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    if file.size > 5_000_000:
        return await interaction.followup.send("❌ File too large.", ephemeral=True)
    try:
        new = json.loads((await file.read()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, discord.HTTPException):
        return await interaction.followup.send("❌ That is not a valid JSON file.", ephemeral=True)
    if not isinstance(new, dict) or not all(k in new for k in ("panels", "keys", "users")):
        return await interaction.followup.send("❌ That is not a valid backup file.", ephemeral=True)
    data.clear()
    data.update(new)
    for k in ("panels", "keys", "users", "banned", "config", "appeals"):
        data.setdefault(k, {})
    save(data)
    for name in data["panels"]:
        bot.add_view(PanelView(name))
    await apply_presence()
    await interaction.followup.send(
        f"✅ Restored **{len(data['panels'])}** panels, **{len(data['keys'])}** keys, "
        f"**{len(data['users'])}** users. Old panel messages work again.",
        ephemeral=True,
    )
    await log_event("♻️ Data restored", f"{interaction.user.mention} restored a backup.", discord.Color.orange())


@bot.tree.command(name="statuspanelbot", description="Set the panel status (Online / Offline)")
@app_commands.describe(status="Online = everything works, Offline = only Get Stats works")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Offline", value="offline"),
    ]
)
@app_commands.default_permissions(administrator=True)
async def status_panel_bot(interaction: discord.Interaction, status: app_commands.Choice[str]):
    data["config"]["panels_online"] = status.value == "online"
    save(data)
    await apply_presence()
    if status.value == "online":
        text = "🟢 Panels are now **online**. All buttons work again."
    else:
        text = "🔴 Panels are now **offline**. Only **Get Stats** can be used."
    await interaction.response.send_message(text, ephemeral=True)
    await log_event(
        "📡 Panel status changed",
        f"{interaction.user.mention} set the panels to **{status.name}**.",
        discord.Color.green() if status.value == "online" else discord.Color.red(),
    )


# ----------------------------- admin API (for the Windows app) ---------
FAILED_AUTH: dict[str, list[float]] = {}
API_RUNNER: web.AppRunner | None = None


def api_ok(**kw):
    return web.json_response({"ok": True, **kw})


def api_err(msg: str, status: int = 400):
    return web.json_response({"ok": False, "error": msg}, status=status)


def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.remote or "?")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    ip = client_ip(request)
    now = time.time()
    fails = [t for t in FAILED_AUTH.get(ip, []) if now - t < 600]
    FAILED_AUTH[ip] = fails
    if len(fails) >= 10:
        return api_err("Too many failed attempts. Try again in 10 minutes.", 429)
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), API_KEY.encode()):
        fails.append(now)
        FAILED_AUTH[ip] = fails
        return api_err("Unauthorized", 401)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # never leak a traceback to the client
        return api_err(f"{type(e).__name__}: {e}", 500)


async def body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


def to_int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def plain_status(entry: dict) -> str:
    if entry.get("banned"):
        return "banned"
    if entry.get("deleted"):
        return "deleted"
    if entry["used_by"] is not None:
        return "redeemed"
    return "unused"


def compute_stats() -> dict:
    now = time.time()
    active = {t: 0 for t in DURATIONS}
    expiring = 0
    redeemed_week = 0
    for panels in data["users"].values():
        for lic in panels.values():
            if lic.get("redeemed_at", 0) > now - 7 * 86400:
                redeemed_week += 1
            if lic.get("deleted"):
                continue
            exp = lic.get("expires_at")
            if exp and exp < now:
                continue
            t = lic.get("duration", "lifetime")
            active[t] = active.get(t, 0) + 1
            if exp and exp - now < 86400:
                expiring += 1
    pending = sum(1 for a in data["appeals"].values() if a.get("status") == "pending")
    return {
        "active": active,
        "expiring_24h": expiring,
        "redeemed_7d": redeemed_week,
        "pending_appeals": pending,
    }


async def h_health(request):
    return web.json_response({"ok": True})


async def h_dashboard(request):
    lat = bot.latency
    keys = data["keys"].values()
    return api_ok(
        panels_online=panels_online(),
        invisible_when_offline=bool(data["config"].get("invisible_when_offline")),
        bot_ready=bot.is_ready(),
        bot_user=str(bot.user) if bot.user else None,
        latency_ms=None if lat != lat else round(lat * 1000),
        uptime=int(time.time() - START_TIME),
        guilds=[{"id": str(g.id), "name": g.name} for g in bot.guilds],
        panels=len(data["panels"]),
        keys_total=len(data["keys"]),
        keys_redeemed=sum(1 for k in keys if k["used_by"] is not None),
        keys_banned=sum(1 for k in keys if k.get("banned")),
        banned_users=len(data["banned"]),
        log_channel=str(data["config"].get("log_channel") or ""),
        stats=compute_stats(),
    )


async def h_set_status(request):
    b = await body(request)
    if "online" not in b:
        return api_err("Missing 'online'.")
    data["config"]["panels_online"] = bool(b["online"])
    if "invisible_when_offline" in b:
        data["config"]["invisible_when_offline"] = bool(b["invisible_when_offline"])
    save(data)
    await apply_presence()
    state = "Online" if panels_online() else "Offline"
    await log_event(
        "📡 Panel status changed",
        f"The admin app set the panels to **{state}**.",
        discord.Color.green() if panels_online() else discord.Color.red(),
    )
    return api_ok(panels_online=panels_online())


async def h_panels(request):
    out = []
    for name, p in data["panels"].items():
        keys = [k for k in data["keys"].values() if k["panel"] == name]
        role_name = None
        for g in bot.guilds:
            r = g.get_role(p.get("role_id") or 0)
            if r:
                role_name = r.name
                break
        out.append({
            "name": name,
            "description": p["description"],
            "role_id": str(p.get("role_id") or ""),
            "role_name": role_name,
            "keys": len(keys),
            "unused": sum(1 for k in keys if plain_status(k) == "unused"),
            "redeemed": sum(1 for k in keys if plain_status(k) == "redeemed"),
            "banned": sum(1 for k in keys if plain_status(k) == "banned"),
            "deleted": sum(1 for k in keys if plain_status(k) == "deleted"),
        })
    return api_ok(panels=out)


async def h_panel_create(request):
    b = await body(request)
    name = str(b.get("name", "")).strip()
    desc = str(b.get("description", "")).strip()
    script = str(b.get("script", ""))
    role_id = to_int(b.get("role_id"))
    if not name or not desc or not script.strip() or role_id is None:
        return api_err("name, description, script and role_id are required.")
    if name in data["panels"]:
        return api_err("A panel with that name already exists.")
    data["panels"][name] = {"description": desc, "script": script, "role_id": role_id}
    save(data)
    bot.add_view(PanelView(name))
    await log_event("🧩 Panel created", f"Admin app created panel **{name}**.")
    return api_ok()


async def h_panel_delete(request):
    b = await body(request)
    name = str(b.get("name", ""))
    if name not in data["panels"]:
        return api_err("Unknown panel.")
    del data["panels"][name]
    data["keys"] = {k: v for k, v in data["keys"].items() if v["panel"] != name}
    for up in data["users"].values():
        up.pop(name, None)
    save(data)
    await log_event("🗑️ Panel deleted", f"Admin app deleted panel **{name}**.", discord.Color.orange())
    return api_ok()


async def h_panel_send(request):
    b = await body(request)
    name = str(b.get("name", ""))
    cid = to_int(b.get("channel_id"))
    if name not in data["panels"]:
        return api_err("Unknown panel.")
    if cid is None:
        return api_err("channel_id is required.")
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return api_err("Channel not found (or the bot cannot see it).")
    try:
        await channel.send(embed=build_panel_embed(name), view=PanelView(name))
    except discord.Forbidden:
        return api_err("The bot may not send messages in that channel.")
    await log_event("📤 Panel sent", f"Admin app posted **{name}** in {channel.mention}.")
    return api_ok()


async def h_keys_create(request):
    b = await body(request)
    panel = str(b.get("panel", ""))
    duration = str(b.get("duration", "")).lower()
    amount = to_int(b.get("amount")) or 1
    user_id = to_int(b.get("user_id")) if str(b.get("user_id", "")).strip() else None
    if panel not in data["panels"]:
        return api_err("Unknown panel.")
    if duration not in DURATIONS:
        return api_err("duration must be daily, weekly, monthly or lifetime.")
    if not 1 <= amount <= 100:
        return api_err("amount must be between 1 and 100.")
    if str(b.get("user_id", "")).strip() and user_id is None:
        return api_err("user_id must be a number.")
    keys = []
    for _ in range(amount):
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        data["keys"][key] = {
            "panel": panel, "used_by": None, "duration": duration,
            "locked_to": user_id, "banned": False, "created_at": int(time.time()),
        }
        keys.append(key)
    save(data)
    await log_event(
        "🔑 Keys created",
        f"Admin app created **{amount}x {duration.capitalize()}** key(s) for **{panel}**"
        + (f", locked to <@{user_id}>." if user_id else "."),
    )
    return api_ok(keys=keys)


async def h_keys_list(request):
    panel = request.query.get("panel", "all")
    status = request.query.get("status", "all")
    out = []
    for key, e in data["keys"].items():
        if panel != "all" and e["panel"] != panel:
            continue
        st = plain_status(e)
        if status != "all" and st != status:
            continue
        out.append({
            "key": key, "panel": e["panel"], "duration": e.get("duration", "lifetime"),
            "status": st, "used_by": str(e["used_by"] or ""),
            "locked_to": str(e.get("locked_to") or ""), "created_at": e.get("created_at"),
        })
    out.sort(key=lambda x: x["created_at"] or 0, reverse=True)
    return api_ok(keys=out)


async def h_keys_revoke(request):
    b = await body(request)
    key = str(b.get("key", "")).strip()
    entry = data["keys"].get(key)
    if not entry:
        return api_err("Key not found.")
    if entry["used_by"] is not None:
        return api_err("That key is already redeemed. Use 'Delete key' with user + panel instead.")
    del data["keys"][key]
    save(data)
    await log_event("🗑️ Unused key removed", f"Admin app removed the unused key `{key[:4]}…`.", discord.Color.orange())
    return api_ok()


async def h_user_info(request):
    b = await body(request)
    uid = to_int(b.get("user_id"))
    if uid is None:
        return api_err("user_id must be a number.")
    name = None
    try:
        name = str(await bot.fetch_user(uid))
    except Exception:
        pass
    lics = []
    for pname, lic in data["users"].get(str(uid), {}).items():
        lics.append({
            "panel": pname, "key": lic["key"], "duration": lic.get("duration", "lifetime"),
            "expires_at": lic.get("expires_at"), "redeemed_at": lic.get("redeemed_at"),
            "deleted": bool(lic.get("deleted")), "hwid_reset_at": lic.get("hwid_reset_at", 0),
        })
    return api_ok(user=name, banned=data["banned"].get(str(uid)), licenses=lics)


def _find_license(b: dict):
    uid = to_int(b.get("user_id"))
    panel = str(b.get("panel", ""))
    if uid is None:
        return None, None, "user_id must be a number."
    if panel not in data["panels"]:
        return None, None, "Unknown panel."
    lic = get_license(uid, panel)
    if not lic or lic.get("deleted"):
        return None, None, "That user has no active key on this panel."
    return uid, lic, None


async def h_user_deletekey(request):
    b = await body(request)
    uid, lic, err = _find_license(b)
    if err:
        return api_err(err)
    panel = str(b["panel"])
    lic["deleted"] = True
    lic["deleted_at"] = int(time.time())
    entry = data["keys"].get(lic["key"])
    if entry:
        entry["deleted"] = True
    save(data)
    await strip_roles(uid, only_panel=panel)
    await log_event("🗑️ Key deleted", f"Admin app deleted the key of <@{uid}> on **{panel}**.", discord.Color.orange())
    return api_ok()


async def h_user_extend(request):
    b = await body(request)
    uid, lic, err = _find_license(b)
    if err:
        return api_err(err)
    days = to_int(b.get("days"))
    if not days or not 1 <= days <= 3650:
        return api_err("days must be between 1 and 3650.")
    if not lic.get("expires_at"):
        return api_err("That is a Lifetime key - nothing to extend.")
    lic["expires_at"] = int(max(lic["expires_at"], time.time()) + days * 86400)
    lic["role_removed"] = False
    save(data)
    await log_event("⏩ Key extended", f"Admin app extended <@{uid}>'s key by {days} day(s).")
    return api_ok(expires_at=lic["expires_at"])


async def h_user_resethwid(request):
    b = await body(request)
    uid, lic, err = _find_license(b)
    if err:
        return api_err(err)
    lic["hwid"] = None
    lic["hwid_reset_at"] = 0
    save(data)
    await log_event("🔄 HWID reset (admin)", f"Admin app reset the HWID of <@{uid}>.", discord.Color.orange())
    return api_ok()


async def h_user_ban(request):
    b = await body(request)
    uid = to_int(b.get("user_id"))
    if uid is None:
        return api_err("user_id must be a number.")
    reason = str(b.get("reason") or "No reason given")
    ban_user_keys(uid, reason)
    await strip_roles(uid)
    extra = ""
    if b.get("ban_from_server"):
        gid = to_int(b.get("guild_id"))
        guild = bot.get_guild(gid) if gid else (bot.guilds[0] if len(bot.guilds) == 1 else None)
        if guild is None:
            return api_err("Keys were banned, but I need a valid guild_id to ban from the server.")
        await dm_banned(uid, guild, reason)  # DM first, then ban
        try:
            await guild.ban(discord.Object(id=uid), reason=f"{BRAND}: {reason}", delete_message_days=0)
            extra = " and banned from the server"
        except (discord.Forbidden, discord.HTTPException):
            extra = " (could not ban from server - missing permission)"
    await log_event("🔨 Key ban", f"Admin app banned <@{uid}>{extra}.\nReason: {reason}", discord.Color.red())
    return api_ok(message=f"Keys banned{extra}.")


async def h_user_unban(request):
    b = await body(request)
    uid = to_int(b.get("user_id"))
    if uid is None:
        return api_err("user_id must be a number.")
    lift_user_ban(uid)
    extra = ""
    if b.get("unban_from_server"):
        gid = to_int(b.get("guild_id"))
        guild = bot.get_guild(gid) if gid else (bot.guilds[0] if len(bot.guilds) == 1 else None)
        if guild is not None:
            try:
                await guild.unban(discord.Object(id=uid), reason=f"{BRAND}: unbanned via admin app")
                extra = " and unbanned from the server"
            except discord.NotFound:
                extra = " (was not banned on the server)"
            except (discord.Forbidden, discord.HTTPException):
                extra = " (could not unban from server - missing permission)"
    await log_event("✅ Key unban", f"Admin app lifted the ban of <@{uid}>{extra}.", discord.Color.green())
    return api_ok(message=f"Key ban lifted{extra}.")


async def h_backup(request):
    return api_ok(data=data)


async def h_restore(request):
    b = await body(request)
    new = b.get("data")
    if not isinstance(new, dict) or not all(k in new for k in ("panels", "keys", "users")):
        return api_err("That is not a valid backup.")
    data.clear()
    data.update(new)
    for k in ("panels", "keys", "users", "banned", "config", "appeals"):
        data.setdefault(k, {})
    save(data)
    for name in data["panels"]:
        bot.add_view(PanelView(name))
    await apply_presence()
    await log_event("♻️ Data restored", "Admin app restored a backup.", discord.Color.orange())
    return api_ok(panels=len(data["panels"]), keys=len(data["keys"]))


def make_api_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=10 * 1024 * 1024)
    app.add_routes([
        web.get("/health", h_health),
        web.get("/api/dashboard", h_dashboard),
        web.post("/api/status", h_set_status),
        web.get("/api/panels", h_panels),
        web.post("/api/panels/create", h_panel_create),
        web.post("/api/panels/delete", h_panel_delete),
        web.post("/api/panels/send", h_panel_send),
        web.post("/api/keys/create", h_keys_create),
        web.get("/api/keys/list", h_keys_list),
        web.post("/api/keys/revoke", h_keys_revoke),
        web.post("/api/users/info", h_user_info),
        web.post("/api/users/deletekey", h_user_deletekey),
        web.post("/api/users/extend", h_user_extend),
        web.post("/api/users/resethwid", h_user_resethwid),
        web.post("/api/users/ban", h_user_ban),
        web.post("/api/users/unban", h_user_unban),
        web.get("/api/backup", h_backup),
        web.post("/api/restore", h_restore),
    ])
    return app


async def start_api():
    global API_RUNNER
    if not API_KEY:
        print("Admin API disabled (set ADMIN_API_KEY to enable the Windows app).")
        return
    if len(API_KEY) < 16:
        print("Admin API disabled: ADMIN_API_KEY must be at least 16 characters.")
        return
    port = int(os.getenv("PORT", "8080"))
    API_RUNNER = web.AppRunner(make_api_app())
    await API_RUNNER.setup()
    await web.TCPSite(API_RUNNER, "0.0.0.0", port).start()
    print(f"Admin API listening on port {port}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    await apply_presence()


bot.run(TOKEN)
