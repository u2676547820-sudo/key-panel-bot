import json
import os
import secrets
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN", "MTU1ODgwOTM4ODc4ODIyNDA0MA.G86l00.Eke8iHk-2ZkevOvNAFtfBPdJOuBDRpR8lpBcgE")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
HWID_COOLDOWN = 24 * 60 * 60  # seconds between HWID resets
BRAND = "BananaHub"

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
    for k in ("panels", "keys", "users", "banned"):
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
    entry = data["keys"].get(lic["key"])
    if entry is None or entry.get("banned"):
        return "🚫 Your key has been banned."
    exp = lic.get("expires_at")
    if exp and exp < time.time():
        return "⌛ Your key has expired."
    return None


def fmt_duration(d: str) -> str:
    return d.capitalize()


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


# ----------------------------- modal -----------------------------------
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
        if get_license(uid, self.panel):
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
        embed.add_field(name="Status", value="🟢 Online", inline=True)
        embed.add_field(name="Latency", value=f"{round(b.latency * 1000)} ms", inline=True)
        embed.add_field(name="Uptime", value=f"{d}d {h}h {m}m {s}s", inline=True)
        embed.add_field(name="Servers", value=str(len(b.guilds)), inline=True)
        embed.add_field(name="Panels", value=str(len(data["panels"])), inline=True)
        embed.add_field(name="Keys", value=f"{used}/{total_keys} redeemed", inline=True)
        embed.add_field(name="Banned keys", value=str(banned), inline=True)

        lic = get_license(interaction.user.id, self.panel)
        if lic:
            exp = lic.get("expires_at")
            embed.add_field(
                name="Your key",
                value=f"{fmt_duration(lic.get('duration', 'lifetime'))} — "
                + (f"expires <t:{exp}:R>" if exp else "lifetime"),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------------------- bot -------------------------------------
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed for leak detection (enable in Dev Portal!)
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for name in data["panels"]:
            self.add_view(PanelView(name))
        expiry_loop.start()
        await self.tree.sync()


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
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None or not message.content:
        return
    content = message.content
    for uid, panels in data["users"].items():
        for lic in panels.values():
            token = lic.get("token")
            if not token or token not in content:
                continue

            owner_id = int(uid)
            member = message.guild.get_member(owner_id)
            # never punish admins (e.g. while testing)
            if member and member.guild_permissions.administrator:
                return

            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass

            ban_user_keys(owner_id, f"Leaked script (found in #{message.channel})")
            await strip_roles(owner_id)
            try:
                await message.guild.ban(
                    discord.Object(id=owner_id),
                    reason=f"{BRAND}: script leak detected",
                    delete_message_days=0,
                )
                outcome = "banned from the server"
            except (discord.Forbidden, discord.HTTPException):
                outcome = "key banned (I lack permission to ban the user)"
            try:
                await message.channel.send(
                    f"🚨 A leaked script was detected. <@{owner_id}> was {outcome}."
                )
            except discord.HTTPException:
                pass
            return


# ----------------------------- commands --------------------------------
async def panel_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=p, value=p)
        for p in data["panels"]
        if current.lower() in p.lower()
    ][:25]


@bot.tree.command(name="setuppanel", description="Create a panel with buttons in this channel")
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
    await interaction.response.defer(ephemeral=True)

    if name in data["panels"]:
        return await interaction.followup.send(
            "❌ A panel with that name already exists. Use `/deletepanel` first.",
            ephemeral=True,
        )

    embed = discord.Embed(title=name, description=description, color=discord.Color.blurple())
    embed.set_footer(text=f"{BRAND} • Redeem your key first to unlock the other buttons.")

    view = PanelView(name)
    try:
        await interaction.channel.send(embed=embed, view=view)
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ I can't send messages in this channel. Give me **View Channel**, "
            "**Send Messages** and **Embed Links** here and try again.",
            ephemeral=True,
        )
    except Exception as e:
        return await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    data["panels"][name] = {"description": description, "script": script, "role_id": role.id}
    save(data)
    bot.add_view(view)
    await interaction.followup.send(f"✅ Panel **{name}** created.", ephemeral=True)


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


@bot.tree.command(name="bankey", description="Ban a user's keys (and optionally the user)")
@app_commands.describe(
    user="The Discord user to ban",
    reason="Reason (optional)",
    ban_from_server="Also ban the user from this server",
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
        try:
            await interaction.guild.ban(user, reason=f"{BRAND}: {reason}", delete_message_days=0)
            extra = " and banned from the server"
        except (discord.Forbidden, discord.HTTPException):
            extra = " (could not ban from server — missing permission)"
    await interaction.followup.send(
        f"🔨 {user.mention}'s keys were banned{extra}.\nReason: {reason}", ephemeral=True
    )


@bot.tree.command(name="unbankey", description="Lift a key ban from a user")
@app_commands.describe(user="The Discord user to unban")
@app_commands.default_permissions(administrator=True)
async def unban_key(interaction: discord.Interaction, user: discord.User):
    data["banned"].pop(str(user.id), None)
    for lic in data["users"].get(str(user.id), {}).values():
        entry = data["keys"].get(lic["key"])
        if entry:
            entry["banned"] = False
    for entry in data["keys"].values():
        if entry.get("locked_to") == user.id:
            entry["banned"] = False
    save(data)
    await interaction.response.send_message(
        f"✅ Key ban lifted for {user.mention}. (Server bans must be lifted in Server Settings → Bans.)",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
