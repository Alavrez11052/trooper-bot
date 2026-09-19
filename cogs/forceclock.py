"""Lets command staff force-end a shift that an officer forgot to (or can't) close themselves."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import BotError, build_embed, fmt_duration, send_log, dm_safe


class ForceClock(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]

    async def _remove_onduty_role(self, member: discord.Member, config) -> None:
        if not config["onduty_role"]:
            return
        role = member.guild.get_role(config["onduty_role"])
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Force clocked out")
            except discord.HTTPException:
                pass

    @app_commands.command(name="forceclockout", description="Force-end an officer's active shift.")
    @app_commands.describe(officer="The officer to clock out.",
                            reason="Why you're forcing this (shown to them).")
    @app_commands.checks.has_permissions(kick_members=True)
    async def forceclockout(self, interaction: discord.Interaction, officer: discord.Member,
                             reason: str = "Ended by command staff.") -> None:
        shift = await self.db.open_shift(interaction.guild_id, officer.id)
        if not shift:
            raise BotError(f"{officer.mention} isn't currently clocked in.")

        duration = await self.db.end_shift(shift["id"])
        config = await self.db.config(interaction.guild_id)
        await self._remove_onduty_role(officer, config)

        embed = build_embed(
            config, title="🔴 Officer Force Clocked Out", color=0xE74C3C,
            description=f"{interaction.user.mention} force-ended {officer.mention}'s shift.")
        embed.add_field(name="Session Length", value=fmt_duration(duration), inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)

        await interaction.response.send_message(embed=embed)
        await send_log(self.bot, config["clock_log_channel"], embed)

        await dm_safe(officer, build_embed(
            config, title="You were clocked out - FLPD",
            description=f"A supervisor ended your shift.\n**Reason:** {reason}", color=0xE74C3C))

    @app_commands.command(name="forceclockout_all",
                           description="Force-end EVERY currently open shift in this server.")
    @app_commands.describe(reason="Why you're clearing all active shifts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def forceclockout_all(self, interaction: discord.Interaction,
                                 reason: str = "Mass clock-out by command staff.") -> None:
        open_shifts = await self.db.all_open_shifts(interaction.guild_id)
        if not open_shifts:
            raise BotError("No one is currently clocked in.")

        await interaction.response.defer()
        config = await self.db.config(interaction.guild_id)
        cleared = []
        for shift in open_shifts:
            duration = await self.db.end_shift(shift["id"])
            member = interaction.guild.get_member(shift["user_id"])
            if member:
                await self._remove_onduty_role(member, config)
            cleared.append((shift["user_id"], duration))

        lines = [f"<@{uid}> - {fmt_duration(dur)}" for uid, dur in cleared]
        embed = build_embed(
            config, title="🔴 Mass Clock-Out", color=0xE74C3C,
            description=f"{interaction.user.mention} force-ended **{len(cleared)}** active shift"
                        f"{'s' if len(cleared) != 1 else ''}.")
        embed.add_field(name="Officers Affected", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)

        await interaction.followup.send(embed=embed)
        await send_log(self.bot, config["clock_log_channel"], embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ForceClock(bot))
