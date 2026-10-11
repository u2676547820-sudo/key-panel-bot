import json
import os
import secrets
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
HWID_COOLDOWN = 24 * 60 * 60  # seconds between HWID resets

START_TIME = time.time()


# ----------------------------- storage ---------------------------------
def load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"panels": {}, "keys": {}, "users": {}}


def save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


data = load()


def get_license(user_id: int, panel: str):
    return data["users"].get(str(user_id), {}).get(panel)


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
        key = self.key_input.value.strip()
        entry = data["keys"].get(key)

        if entry is None or entry["panel"] != self.panel:
            return await interaction.response.send_message(
                "❌ Invalid key for this panel.", ephemeral=True
            )
        if entry["used_by"] is not None:
            return await interaction.response.send_message(
                "❌ This key has already been redeemed.", ephemeral=True
            )
        if get_license(interaction.user.id, self.panel):
            return await interaction.response.send_message(
                "⚠️ You already have a key redeemed on this panel.", ephemeral=True
            )

        entry["used_by"] = interaction.user.id
        data["users"].setdefault(str(interaction.user.id), {})[self.panel] = {
            "key": key,
            "hwid_reset_at": 0,
            "redeemed_at": int(time.time()),
        }
        save(data)
        await interaction.response.send_message(
            "✅ Key redeemed successfully! You can now use **Get Script**, "
            "**Get Role** and **Reset HWID**.",
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
        if get_license(interaction.user.id, self.panel):
            return True
        await interaction.response.send_message(
            "🔒 You need to **Redeem Key** first!", ephemeral=True
        )
        return False

    async def redeem(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RedeemModal(self.panel))

    async def script(self, interaction: discord.Interaction):
        if not await self._require_key(interaction):
            return
        script = data["panels"][self.panel]["script"]
        await interaction.response.send_message(
            f"📄 **Your script:**\n```lua\n{script}\n```", ephemeral=True
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
        bot: commands.Bot = interaction.client
        total_keys = len(data["keys"])
        used = sum(1 for k in data["keys"].values() if k["used_by"] is not None)
        uptime = int(time.time() - START_TIME)
        d, r = divmod(uptime, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)

        embed = discord.Embed(title="📊 Bot Stats", color=discord.Color.blurple())
        embed.add_field(name="Status", value="🟢 Online", inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
        embed.add_field(name="Uptime", value=f"{d}d {h}h {m}m {s}s", inline=True)
        embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="Panels", value=str(len(data["panels"])), inline=True)
        embed.add_field(name="Keys", value=f"{used}/{total_keys} redeemed", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ----------------------------- bot -------------------------------------
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        for name in data["panels"]:
            self.add_view(PanelView(name))  # re-register persistent views
        await self.tree.sync()


bot = Bot()


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
    if name in data["panels"]:
        return await interaction.response.send_message(
            "❌ A panel with that name already exists.", ephemeral=True
        )

    data["panels"][name] = {"description": description, "script": script, "role_id": role.id}
    save(data)

    embed = discord.Embed(
        title=name, description=description, color=discord.Color.blurple()
    )
    embed.set_footer(text="Redeem your key first to unlock the other buttons.")

    view = PanelView(name)
    bot.add_view(view)
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Panel **{name}** created.", ephemeral=True)


@bot.tree.command(name="createkey", description="Generate keys for a panel")
@app_commands.describe(panel="Which panel the key is for", amount="How many keys (1-25)")
@app_commands.autocomplete(panel=panel_autocomplete)
@app_commands.default_permissions(administrator=True)
async def create_key(
    interaction: discord.Interaction,
    panel: str,
    amount: app_commands.Range[int, 1, 25] = 1,
):
    if panel not in data["panels"]:
        return await interaction.response.send_message("❌ Unknown panel.", ephemeral=True)

    keys = []
    for _ in range(amount):
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        data["keys"][key] = {"panel": panel, "used_by": None}
        keys.append(key)
    save(data)

    await interaction.response.send_message(
        f"🔑 **{amount} key(s) for {panel}:**\n```\n" + "\n".join(keys) + "\n```",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


bot.run(TOKEN)
