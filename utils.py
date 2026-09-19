"""Shared helpers: duration parsing, formatting, and the configurable embed builder."""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

DEFAULT_COLOR = 0x0A2F5C  # FLPD navy - overridden instantly by /config embed color


class BotError(Exception):
    """Raised for user-facing failures; the global error handler renders these cleanly."""


def parse_duration(text: str) -> int:
    """'1h30m' / '90m' / '2d' -> seconds. A bare number is treated as minutes."""
    text = text.strip().lower()
    if not text:
        raise BotError("No duration given.")
    if text.isdigit():
        return int(text) * 60

    compact = re.sub(r"\s+", "", text)
    matches = DURATION_RE.findall(compact)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != compact:
        raise BotError("Couldn't read that duration. Try `30m`, `2h`, `1h30m`, or `3d`.")

    return sum(int(n) * _UNITS[u] for n, u in matches)


def fmt_duration(seconds: int) -> str:
    """3900 -> '1h 5m'. Handles negatives (e.g. for time-fix deductions)."""
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return sign + " ".join(parts)


def ts(epoch: int, style: str = "f") -> str:
    """Discord dynamic timestamp markup - renders in each viewer's own timezone."""
    return f"<t:{int(epoch)}:{style}>"


def build_embed(
    config=None,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    color: Optional[int] = None,
    url: Optional[str] = None,
) -> discord.Embed:
    """Every embed the bot sends goes through here, so /config instantly restyles all of them."""
    resolved_color = color
    if resolved_color is None and config is not None:
        resolved_color = config["embed_color"]
    if resolved_color is None:
        resolved_color = DEFAULT_COLOR

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color(int(resolved_color)),
        url=url,
        timestamp=discord.utils.utcnow(),
    )

    footer_text = config["embed_footer"] if config is not None else None
    footer_icon = config["embed_icon"] if config is not None else None
    if footer_text or footer_icon:
        embed.set_footer(text=footer_text or "FLPD", icon_url=footer_icon or None)
    else:
        embed.set_footer(text="FLPD")

    thumb = config["embed_thumbnail"] if config is not None else None
    if thumb:
        embed.set_thumbnail(url=thumb)

    return embed


def parse_color(value: str) -> int:
    """Accepts '#5865F2', '5865F2', '0x5865F2', or a named discord.Color (e.g. 'blurple')."""
    value = value.strip()
    hex_part = value.lstrip("#")

    named = getattr(discord.Color, value.lower().replace(" ", "_").replace("#", ""), None)
    if callable(named):
        try:
            result = named()
            if isinstance(result, discord.Color):
                return result.value
        except TypeError:
            pass
    try:
        return int(hex_part, 16)
    except ValueError:
        raise BotError("That isn't a valid colour. Try a hex code like `#0A2F5C`.") from None


async def send_log(bot: discord.Client, channel_id: Optional[int], embed: discord.Embed) -> None:
    """Best-effort log delivery - a missing/deleted channel should never break a command."""
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


async def dm_safe(user: discord.abc.Snowflake, embed: discord.Embed) -> bool:
    """Best-effort DM; returns whether it landed (many users lock DMs)."""
    try:
        await user.send(embed=embed)  # type: ignore[attr-defined]
        return True
    except discord.HTTPException:
        return False


async def send_error(interaction: discord.Interaction, message: str) -> None:
    """Shows an ephemeral error to the user regardless of whether we've responded yet."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def _describe_error(error: Exception) -> str:
    original = getattr(error, "original", error)
    if isinstance(original, BotError):
        return str(original)
    logging.getLogger("flpd-bot").exception("Unhandled component error", exc_info=error)
    return "Something went wrong running that. It's been logged - try again in a moment."


class SafeView(discord.ui.View):
    """Base for every button view in the bot. Without this, a BotError (or any exception)
    raised inside a button callback never reaches the user - Discord just shows a generic
    'something went wrong' with nothing in the logs pointing at why."""

    async def on_error(self, interaction: discord.Interaction, error: Exception,
                        item: discord.ui.Item) -> None:
        await send_error(interaction, _describe_error(error))


class SafeModal(discord.ui.Modal):
    """Base for every modal in the bot - see SafeView for why this matters."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_error(interaction, _describe_error(error))