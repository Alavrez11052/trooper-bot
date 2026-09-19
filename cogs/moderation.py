from __future__ import annotations

from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import BotError, build_embed, parse_duration, fmt_duration, send_log, dm_safe


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]

    async def _log(self, guild_id: int, embed: discord.Embed) -> None:
        config = await self.db.config(guild_id)
        await send_log(self.bot, config["mod_log_channel"], embed)

    def _check_hierarchy(self, actor: discord.Member, target: discord.Member) -> None:
        if target.id == actor.id:
            raise BotError("You can't target yourself.")
        if target.id == actor.guild.owner_id:
            raise BotError("You can't target the server owner.")
        if target.top_role >= actor.top_role and actor.id != actor.guild.owner_id:
            raise BotError("You can't act on someone with an equal or higher role than you.")
        if target.top_role >= actor.guild.me.top_role:
            raise BotError("My role is too low to act on that member. Move my role higher.")

    async def _case(self, interaction: discord.Interaction, title: str, color: int,
                     fields: list[tuple[str, str, bool]], respond: bool = True) -> discord.Embed:
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title=title, color=color)
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        if respond:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)
        await self._log(interaction.guild_id, embed)
        return embed

    # ==================== MEMBER: kick / ban ====================

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Who to kick.", reason="Why they're being kicked.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member,
                    reason: str = "No reason provided.") -> None:
        self._check_hierarchy(interaction.user, member)  # type: ignore[arg-type]
        config = await self.db.config(interaction.guild_id)
        await dm_safe(member, build_embed(config, title="You were kicked - FLPD",
                                           description=f"**Reason:** {reason}", color=0xE67E22))
        await member.kick(reason=f"{interaction.user}: {reason}")
        await self._case(interaction, "👢 Member Kicked", 0xE67E22, [
            ("Member", f"{member.mention} ({member.id})", False),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, True),
        ])

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(member="Who to ban.", reason="Why they're being banned.",
                            delete_days="Days of their message history to delete (0-7).")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member,
                   reason: str = "No reason provided.",
                   delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        self._check_hierarchy(interaction.user, member)  # type: ignore[arg-type]
        config = await self.db.config(interaction.guild_id)
        await dm_safe(member, build_embed(config, title="You were banned - FLPD",
                                           description=f"**Reason:** {reason}", color=0xC0392B))
        await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
        await self._case(interaction, "🔨 Member Banned", 0xC0392B, [
            ("Member", f"{member.mention} ({member.id})", False),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, True),
        ])

    @app_commands.command(name="softban", description="Ban then immediately unban - clears recent messages without a lasting ban.")
    @app_commands.describe(member="Who to softban.", reason="Reason.",
                            delete_days="Days of message history to delete (0-7).")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def softban(self, interaction: discord.Interaction, member: discord.Member,
                       reason: str = "No reason provided.",
                       delete_days: app_commands.Range[int, 0, 7] = 1) -> None:
        self._check_hierarchy(interaction.user, member)  # type: ignore[arg-type]
        await member.ban(reason=f"Softban: {interaction.user}: {reason}", delete_message_days=delete_days)
        await interaction.guild.unban(member, reason="Softban - auto unban")
        await self._case(interaction, "🧹 Member Softbanned", 0xD35400, [
            ("Member", f"{member.mention} ({member.id})", False),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, True),
        ])

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="The user's ID.", reason="Why they're being unbanned.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str,
                     reason: str = "No reason provided.") -> None:
        if not user_id.strip().isdigit():
            raise BotError("That doesn't look like a valid user ID.")
        uid = int(user_id.strip())
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=f"{interaction.user}: {reason}")
        except discord.NotFound:
            raise BotError("That user isn't banned.") from None
        await self._case(interaction, "🔓 Member Unbanned", 0x2ECC71, [
            ("User ID", str(uid), True),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, False),
        ])

    @app_commands.command(name="massban", description="Ban multiple users at once by ID.")
    @app_commands.describe(user_ids="Space or comma separated user IDs.", reason="Reason for all bans.")
    @app_commands.checks.has_permissions(ban_members=True, administrator=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def massban(self, interaction: discord.Interaction, user_ids: str,
                       reason: str = "Mass ban.") -> None:
        raw = user_ids.replace(",", " ").split()
        ids = [int(x) for x in raw if x.isdigit()]
        if not ids:
            raise BotError("No valid user IDs found.")

        await interaction.response.defer()
        banned, failed = [], []
        for uid in ids:
            try:
                await interaction.guild.ban(discord.Object(id=uid), reason=f"{interaction.user}: {reason}")
                banned.append(uid)
            except discord.HTTPException:
                failed.append(uid)

        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title="🔨 Mass Ban Complete", color=0xC0392B)
        embed.add_field(name="Banned", value=str(len(banned)), inline=True)
        embed.add_field(name="Failed", value=str(len(failed)), inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.followup.send(embed=embed)
        await self._log(interaction.guild_id, embed)

    # ==================== MEMBER: timeout ====================

    @app_commands.command(name="timeout", description="Time out a member (mutes them for a duration).")
    @app_commands.describe(member="Who to time out.", duration="e.g. 10m, 1h, 1d (max 28d).",
                            reason="Why they're being timed out.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                       duration: str, reason: str = "No reason provided.") -> None:
        self._check_hierarchy(interaction.user, member)  # type: ignore[arg-type]
        seconds = parse_duration(duration)
        if seconds > 28 * 86400:
            raise BotError("Timeouts can't exceed 28 days.")
        until = discord.utils.utcnow() + timedelta(seconds=seconds)
        await member.timeout(until, reason=f"{interaction.user}: {reason}")
        await self._case(interaction, "🔇 Member Timed Out", 0xF39C12, [
            ("Member", member.mention, True),
            ("Duration", fmt_duration(seconds), True),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, False),
        ])

    @app_commands.command(name="untimeout", description="Remove an active timeout from a member.")
    @app_commands.describe(member="Who to remove the timeout from.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not member.is_timed_out():
            raise BotError("That member isn't timed out.")
        await member.timeout(None, reason=f"Removed by {interaction.user}")
        await self._case(interaction, "🔊 Timeout Removed", 0x2ECC71, [
            ("Member", member.mention, True),
            ("Moderator", interaction.user.mention, True),
        ])

    # ==================== MEMBER: nickname / roles ====================

    @app_commands.command(name="nickname", description="Change a member's nickname (leave blank to reset).")
    @app_commands.describe(member="Who to rename.", nickname="New nickname - leave empty to reset.")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    async def nickname(self, interaction: discord.Interaction, member: discord.Member,
                        nickname: Optional[str] = None) -> None:
        self._check_hierarchy(interaction.user, member)  # type: ignore[arg-type]
        old = member.display_name
        await member.edit(nick=nickname, reason=f"Changed by {interaction.user}")
        await self._case(interaction, "✏️ Nickname Changed", 0x3498DB, [
            ("Member", member.mention, True),
            ("Before", old, True),
            ("After", nickname or member.name, True),
        ])

    @app_commands.command(name="role_add", description="Add a role to a member.")
    @app_commands.describe(member="Who to give the role to.", role="The role to add.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def role_add(self, interaction: discord.Interaction, member: discord.Member,
                        role: discord.Role) -> None:
        if role >= interaction.guild.me.top_role:
            raise BotError("My role is below that role - move my role higher.")
        if role in member.roles:
            raise BotError(f"{member.mention} already has {role.mention}.")
        await member.add_roles(role, reason=f"Added by {interaction.user}")
        await self._case(interaction, "➕ Role Added", 0x2ECC71, [
            ("Member", member.mention, True),
            ("Role", role.mention, True),
            ("Moderator", interaction.user.mention, True),
        ])

    @app_commands.command(name="role_remove", description="Remove a role from a member.")
    @app_commands.describe(member="Who to remove the role from.", role="The role to remove.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member,
                           role: discord.Role) -> None:
        if role >= interaction.guild.me.top_role:
            raise BotError("My role is below that role - move my role higher.")
        if role not in member.roles:
            raise BotError(f"{member.mention} doesn't have {role.mention}.")
        await member.remove_roles(role, reason=f"Removed by {interaction.user}")
        await self._case(interaction, "➖ Role Removed", 0xE74C3C, [
            ("Member", member.mention, True),
            ("Role", role.mention, True),
            ("Moderator", interaction.user.mention, True),
        ])

    # ==================== MESSAGES ====================

    @app_commands.command(name="purge", description="Bulk delete recent messages in this channel.")
    @app_commands.describe(amount="How many messages to delete (1-100).",
                            member="Only delete messages from this member.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction,
                     amount: app_commands.Range[int, 1, 100],
                     member: Optional[discord.Member] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        deleted = await interaction.channel.purge(limit=amount, check=check)
        await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(
            config, title="🧹 Messages Purged", color=0x95A5A6,
            description=f"{interaction.user.mention} deleted **{len(deleted)}** messages in "
                        f"{interaction.channel.mention}" + (f" from {member.mention}" if member else ""))
        await self._log(interaction.guild_id, embed)

    @app_commands.command(name="purge_bots", description="Delete recent messages sent by bots in this channel.")
    @app_commands.describe(amount="How many messages to scan (1-100).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_bots(self, interaction: discord.Interaction,
                          amount: app_commands.Range[int, 1, 100] = 50) -> None:
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.author.bot)
        await interaction.followup.send(f"Deleted {len(deleted)} bot messages.", ephemeral=True)
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title="🧹 Bot Messages Purged", color=0x95A5A6,
                             description=f"{interaction.user.mention} cleared {len(deleted)} bot "
                                         f"messages in {interaction.channel.mention}.")
        await self._log(interaction.guild_id, embed)

    @app_commands.command(name="purge_contains", description="Delete recent messages containing specific text.")
    @app_commands.describe(text="Text to match (case-insensitive).", amount="How many messages to scan (1-100).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    async def purge_contains(self, interaction: discord.Interaction, text: str,
                              amount: app_commands.Range[int, 1, 100] = 50) -> None:
        await interaction.response.defer(ephemeral=True)
        needle = text.lower()
        deleted = await interaction.channel.purge(limit=amount, check=lambda m: needle in m.content.lower())
        await interaction.followup.send(f"Deleted {len(deleted)} matching messages.", ephemeral=True)
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title="🧹 Messages Purged (text match)", color=0x95A5A6,
                             description=f"{interaction.user.mention} deleted {len(deleted)} messages "
                                         f"containing `{text}` in {interaction.channel.mention}.")
        await self._log(interaction.guild_id, embed)

    # ==================== CHANNEL ====================

    @app_commands.command(name="slowmode", description="Set slowmode for this channel.")
    @app_commands.describe(seconds="Delay between messages, in seconds (0 to disable).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction,
                        seconds: app_commands.Range[int, 0, 21600]) -> None:
        await interaction.channel.edit(slowmode_delay=seconds)
        desc = (f"Slowmode set to **{seconds}s** in {interaction.channel.mention}." if seconds
                else f"Slowmode disabled in {interaction.channel.mention}.")
        await self._case(interaction, "🐌 Slowmode Updated", 0x3498DB, [("Detail", desc, False)])

    @app_commands.command(name="lock", description="Lock this channel (prevents @everyone from sending messages).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(self, interaction: discord.Interaction, reason: str = "No reason provided.") -> None:
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                                    reason=f"{interaction.user}: {reason}")
        await self._case(interaction, "🔒 Channel Locked", 0xC0392B, [
            ("Channel", interaction.channel.mention, True),
            ("Moderator", interaction.user.mention, True),
            ("Reason", reason, False),
        ])

    @app_commands.command(name="unlock", description="Unlock this channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(self, interaction: discord.Interaction) -> None:
        overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                                    reason=f"Unlocked by {interaction.user}")
        await self._case(interaction, "🔓 Channel Unlocked", 0x2ECC71, [
            ("Channel", interaction.channel.mention, True),
            ("Moderator", interaction.user.mention, True),
        ])

    @app_commands.command(name="slowmode_off_all", description="Disable slowmode in every text channel on the server.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode_off_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        count = 0
        for channel in interaction.guild.text_channels:
            if channel.slowmode_delay:
                try:
                    await channel.edit(slowmode_delay=0, reason=f"Cleared by {interaction.user}")
                    count += 1
                except discord.HTTPException:
                    pass
        await self._case(interaction, "🐌 Slowmode Cleared Server-Wide", 0x3498DB, [
            ("Channels Updated", str(count), True),
            ("Moderator", interaction.user.mention, True),
        ], respond=False)
        await interaction.followup.send(f"Cleared slowmode in {count} channel(s).")

    # ==================== VOICE ====================

    @app_commands.command(name="voice_disconnect", description="Disconnect a member from voice chat.")
    @app_commands.describe(member="Who to disconnect.")
    @app_commands.checks.has_permissions(move_members=True)
    @app_commands.checks.bot_has_permissions(move_members=True)
    async def voice_disconnect(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not member.voice:
            raise BotError(f"{member.mention} isn't in a voice channel.")
        await member.move_to(None, reason=f"Disconnected by {interaction.user}")
        await self._case(interaction, "🔌 Disconnected From Voice", 0xE74C3C, [
            ("Member", member.mention, True),
            ("Moderator", interaction.user.mention, True),
        ])

    @app_commands.command(name="voice_move", description="Move a member to a different voice channel.")
    @app_commands.describe(member="Who to move.", channel="The voice channel to move them to.")
    @app_commands.checks.has_permissions(move_members=True)
    @app_commands.checks.bot_has_permissions(move_members=True)
    async def voice_move(self, interaction: discord.Interaction, member: discord.Member,
                          channel: discord.VoiceChannel) -> None:
        if not member.voice:
            raise BotError(f"{member.mention} isn't in a voice channel.")
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
        await self._case(interaction, "🔀 Moved Voice Channel", 0x3498DB, [
            ("Member", member.mention, True),
            ("New Channel", channel.mention, True),
        ])

    @app_commands.command(name="voice_mute", description="Server voice-mute a member.")
    @app_commands.checks.has_permissions(mute_members=True)
    @app_commands.checks.bot_has_permissions(mute_members=True)
    async def voice_mute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await member.edit(mute=True, reason=f"Muted by {interaction.user}")
        await self._case(interaction, "🔇 Voice Muted", 0xE74C3C, [("Member", member.mention, True)])

    @app_commands.command(name="voice_unmute", description="Remove a member's server voice-mute.")
    @app_commands.checks.has_permissions(mute_members=True)
    @app_commands.checks.bot_has_permissions(mute_members=True)
    async def voice_unmute(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await member.edit(mute=False, reason=f"Unmuted by {interaction.user}")
        await self._case(interaction, "🔊 Voice Unmuted", 0x2ECC71, [("Member", member.mention, True)])

    @app_commands.command(name="voice_deafen", description="Server voice-deafen a member.")
    @app_commands.checks.has_permissions(deafen_members=True)
    @app_commands.checks.bot_has_permissions(deafen_members=True)
    async def voice_deafen(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await member.edit(deafen=True, reason=f"Deafened by {interaction.user}")
        await self._case(interaction, "🔇 Voice Deafened", 0xE74C3C, [("Member", member.mention, True)])

    @app_commands.command(name="voice_undeafen", description="Remove a member's server voice-deafen.")
    @app_commands.checks.has_permissions(deafen_members=True)
    @app_commands.checks.bot_has_permissions(deafen_members=True)
    async def voice_undeafen(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await member.edit(deafen=False, reason=f"Undeafened by {interaction.user}")
        await self._case(interaction, "🔊 Voice Undeafened", 0x2ECC71, [("Member", member.mention, True)])

    # ==================== NOTES ====================

    @app_commands.command(name="note", description="Leave an internal mod note on a member (posted to mod-log only).")
    @app_commands.describe(member="Who the note is about.", note="The note content.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def note(self, interaction: discord.Interaction, member: discord.Member, note: str) -> None:
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title="🗒️ Mod Note", color=0x7F8C8D)
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Note", value=note, inline=False)
        await interaction.response.send_message("Note logged.", ephemeral=True)
        await self._log(interaction.guild_id, embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))