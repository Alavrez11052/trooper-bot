"""FLPD Discord Bot - entrypoint.

Run with:  python bot.py
Requires a .env file (see .env.example) with DISCORD_TOKEN set.
"""

from __future__ import annotations

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from db import Database

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("flpd-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "flpd.sqlite3")
GUILD_ID = os.getenv("GUILD_ID")  # optional: instant command sync to one server while testing

INTENTS = discord.Intents.default()
INTENTS.members = True  # needed to resolve officers for roles / DMs / record lookups
INTENTS.message_content = False


class FLPDBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=INTENTS, help_command=None)
        self.db = Database(DB_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("Database connected at %s", DB_PATH)

        for ext in ("cogs.clock", "cogs.moderation", "cogs.discipline", "cogs.config"):
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        # Re-register the persistent clock panel view so buttons keep working after a restart.
        from cogs.clock import ClockPanelView
        self.add_view(ClockPanelView(self))

        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            log.info("Synced %d commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="FLPD - Administration"))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


bot = FLPDBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception) -> None:
    from discord import app_commands
    from utils import BotError

    original = getattr(error, "original", error)

    if isinstance(original, BotError):
        message = str(original)
    elif isinstance(error, app_commands.MissingPermissions):
        message = "You don't have permission to use that command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Slow down - try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You can't use that command here."
    else:
        log.exception("Unhandled app command error", exc_info=error)
        message = "Something went wrong running that command. It's been logged."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN, log_handler=None)
