"""FLPD disciplinary system: warnings, strikes, suspensions, terminations, and records."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import BotError, build_embed, fmt_duration, parse_duration, send_log, dm_safe, ts

ACTION_COLORS = {
    "Verbal Warning": 0xF1C40F,
    "Written Warning": 0xE67E22,
    "Strike": 0xE74C3C,
    "Suspension": 0x992D22,
    "Termination": 0x1C1C1C,
    "Blacklist": 0x000000,
}


class Discipline(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]
        self.suspension_watcher.start()

    def cog_unload(self) -> None:
        self.suspension_watcher.cancel()

    async def _log(self, guild_id: int, embed: discord.Embed) -> None:
        config = await self.db.config(guild_id)
        await send_log(self.bot, config["discipline_log_channel"], embed)

    async def _record_action(
        self, interaction: discord.Interaction, member: discord.Member, action: str,
        reason: str, expires_ts: Optional[int] = None, role_applied: Optional[int] = None,
    ) -> int:
        entry_id = await self.db.add_discipline(
            interaction.guild_id, member.id, action, reason, interaction.user.id,
            expires_ts, role_applied)

        config = await self.db.config(interaction.guild_id)
        color = ACTION_COLORS.get(action, 0x7F8C8D)

        public_embed = build_embed(config, title=f"⚖️ {action} Issued", color=color)
        public_embed.add_field(name="Officer", value=member.mention, inline=True)
        public_embed.add_field(name="Issued By", value=interaction.user.mention, inline=True)
        if expires_ts:
            public_embed.add_field(name="Expires", value=ts(expires_ts, "R"), inline=True)
        public_embed.add_field(name="Reason", value=reason, inline=False)
        public_embed.set_footer(text=f"Case #{entry_id}")

        dm_embed = build_embed(
            config, title=f"You received a {action.lower()} - FLPD",
            description=f"**Reason:** {reason}", color=color)
        if expires_ts:
            dm_embed.add_field(name="Expires", value=ts(expires_ts, "F"))
        await dm_safe(member, dm_embed)

        if interaction.response.is_done():
            await interaction.followup.send(embed=public_embed)
        else:
            await interaction.response.send_message(embed=public_embed)
        await self._log(interaction.guild_id, public_embed)
        return entry_id

    # ---------------- ladder commands ----------------

    @app_commands.command(name="warn", description="Issue a warning to an officer.")
    @app_commands.describe(officer="The officer being warned.", reason="Reason for the warning.",
                            written="Mark this as a Written Warning instead of Verbal.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def warn(self, interaction: discord.Interaction, officer: discord.Member,
                    reason: str, written: bool = False) -> None:
        action = "Written Warning" if written else "Verbal Warning"
        await self._record_action(interaction, officer, action, reason)

    @app_commands.command(name="strike", description="Issue a formal strike to an officer.")
    @app_commands.describe(officer="The officer receiving the strike.", reason="Reason for the strike.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def strike(self, interaction: discord.Interaction, officer: discord.Member, reason: str) -> None:
        await self._record_action(interaction, officer, "Strike", reason)

    @app_commands.command(name="suspend", description="Suspend an officer for a set duration.")
    @app_commands.describe(officer="The officer being suspended.", duration="e.g. 3d, 1w, 12h.",
                            reason="Reason for the suspension.",
                            suspended_role="Role to apply for the duration (removed on expiry).")
    @app_commands.checks.has_permissions(kick_members=True)
    async def suspend(self, interaction: discord.Interaction, officer: discord.Member,
                       duration: str, reason: str,
                       suspended_role: Optional[discord.Role] = None) -> None:
        seconds = parse_duration(duration)
        expires_ts = int((discord.utils.utcnow() + timedelta(seconds=seconds)).timestamp())

        role_id = None
        if suspended_role:
            try:
                await officer.add_roles(suspended_role, reason=f"Suspended: {reason}")
                role_id = suspended_role.id
            except discord.HTTPException:
                pass

        await self._record_action(interaction, officer, "Suspension", reason, expires_ts, role_id)

    @app_commands.command(name="terminate", description="Terminate an officer from FLPD.")
    @app_commands.describe(officer="The officer being terminated.", reason="Reason for termination.",
                            kick="Also kick them from the server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def terminate(self, interaction: discord.Interaction, officer: discord.Member,
                         reason: str, kick: bool = False) -> None:
        await self._record_action(interaction, officer, "Termination", reason)
        if kick:
            try:
                await officer.kick(reason=f"Terminated by {interaction.user}: {reason}")
            except discord.HTTPException:
                await interaction.followup.send(
                    "⚠️ Termination logged, but I couldn't kick them (check my role position).",
                    ephemeral=True)

    @app_commands.command(name="blacklist", description="Blacklist a user from FLPD entirely (ban + record).")
    @app_commands.describe(officer="The user being blacklisted.", reason="Reason for the blacklist.")
    @app_commands.checks.has_permissions(administrator=True)
    async def blacklist(self, interaction: discord.Interaction, officer: discord.Member, reason: str) -> None:
        await self._record_action(interaction, officer, "Blacklist", reason)
        try:
            await officer.ban(reason=f"Blacklisted by {interaction.user}: {reason}")
        except discord.HTTPException:
            await interaction.followup.send(
                "⚠️ Blacklist logged, but I couldn't ban them (check my role position).", ephemeral=True)

    # ---------------- record management ----------------

    @app_commands.command(name="record", description="View an officer's disciplinary record.")
    @app_commands.describe(officer="The officer to look up.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def record(self, interaction: discord.Interaction, officer: discord.Member) -> None:
        entries = await self.db.record(interaction.guild_id, officer.id)
        config = await self.db.config(interaction.guild_id)

        embed = build_embed(config, title=f"📋 Disciplinary Record - {officer.display_name}")
        if not entries:
            embed.description = "Clean record - no entries on file."
        else:
            lines = []
            for e in entries[:15]:
                status = "🟢 Active" if e["active"] else "⚪ Voided/Expired"
                line = f"**#{e['id']} - {e['action']}** ({status}) - {ts(e['created_ts'], 'd')}\n> {e['reason']}"
                lines.append(line)
            embed.description = "\n\n".join(lines)
            embed.set_footer(text=f"{len(entries)} total entr{'y' if len(entries)==1 else 'ies'}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="void", description="Void a disciplinary entry (e.g. issued in error, appeal won).")
    @app_commands.describe(case_id="The case number from /record.", reason="Why it's being voided.")
    @app_commands.checks.has_permissions(administrator=True)
    async def void(self, interaction: discord.Interaction, case_id: int, reason: str) -> None:
        entry = await self.db.get_discipline(case_id)
        if entry is None or entry["guild_id"] != interaction.guild_id:
            raise BotError("No case found with that ID in this server.")
        if not entry["active"]:
            raise BotError("That case is already inactive.")

        await self.db.void_discipline(case_id, interaction.user.id, reason)

        if entry["action"] == "Suspension" and entry["role_applied"]:
            member = interaction.guild.get_member(entry["user_id"])
            role = interaction.guild.get_role(entry["role_applied"])
            if member and role:
                try:
                    await member.remove_roles(role, reason="Suspension voided")
                except discord.HTTPException:
                    pass

        config = await self.db.config(interaction.guild_id)
        embed = build_embed(
            config, title="Case Voided", color=0x95A5A6,
            description=f"Case **#{case_id}** ({entry['action']}) for <@{entry['user_id']}> "
                        f"was voided by {interaction.user.mention}.")
        embed.add_field(name="Void Reason", value=reason)
        await interaction.response.send_message(embed=embed)
        await self._log(interaction.guild_id, embed)

    # ---------------- background: auto-lift expired suspensions ----------------

    @tasks.loop(minutes=5)
    async def suspension_watcher(self) -> None:
        for entry in await self.db.expired_suspensions():
            guild = self.bot.get_guild(entry["guild_id"])
            if guild is None:
                continue
            await self.db.lift_suspension(entry["id"])

            member = guild.get_member(entry["user_id"])
            if member and entry["role_applied"]:
                role = guild.get_role(entry["role_applied"])
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Suspension expired")
                    except discord.HTTPException:
                        pass

            config = await self.db.config(entry["guild_id"])
            embed = build_embed(
                config, title="Suspension Expired", color=0x2ECC71,
                description=f"<@{entry['user_id']}>'s suspension (case #{entry['id']}) has expired "
                            "and any suspension role was removed.")
            await self._log(entry["guild_id"], embed)

    @suspension_watcher.before_loop
    async def before_watcher(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Discipline(bot))
