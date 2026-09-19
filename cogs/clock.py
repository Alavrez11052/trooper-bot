"""Clock in/out system: persistent panel, My Time view, and time-fix requests."""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import BotError, build_embed, fmt_duration, parse_duration, send_log, dm_safe, SafeView, SafeModal

SUPERVISOR_PERMS = dict(kick_members=True)  # threshold for approving time-fix requests


def is_supervisor(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.kick_members


class TimeFixModal(SafeModal, title="Request a Time Fix"):
    amount = discord.ui.TextInput(
        label="Amount (e.g. 30m, 1h, -20m)",
        placeholder="Use a leading - to request time REMOVED",
        max_length=20,
    )
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, cog: "Clock") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip()
        negative = raw.startswith("-")
        seconds = parse_duration(raw.lstrip("-").strip())
        minutes = -(seconds // 60) if negative else seconds // 60
        if minutes == 0:
            raise BotError("That amount rounds to 0 minutes - try something like `15m` or `1h`.")

        await self.cog.submit_timefix(interaction, minutes, self.reason.value.strip())


class TimeFixReviewView(SafeView):
    """Persistent-style view attached to the request message in the review channel."""

    def __init__(self, cog: "Clock", request_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_supervisor(interaction.user):
            await interaction.response.send_message(
                "You need supervisor permissions (Kick Members or higher) to review this.",
                ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="flpd:timefix:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await self.cog.resolve_timefix(interaction, self.request_id, approve=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="flpd:timefix:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await self.cog.resolve_timefix(interaction, self.request_id, approve=False)


class ClockPanelView(SafeView):
    """The persistent panel posted with /clockpanel. custom_ids are fixed so it survives restarts."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @property
    def cog(self) -> "Clock":
        return self.bot.get_cog("Clock")  # type: ignore[return-value]

    @discord.ui.button(label="Clock In", style=discord.ButtonStyle.secondary,
                        emoji="✅", custom_id="flpd:clock:in")
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_clock_in(interaction)

    @discord.ui.button(label="Clock Out", style=discord.ButtonStyle.secondary,
                        emoji="❌", custom_id="flpd:clock:out")
    async def clock_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_clock_out(interaction)

    @discord.ui.button(label="My Time", style=discord.ButtonStyle.secondary,
                        emoji="⏱️", custom_id="flpd:clock:mytime")
    async def my_time(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_my_time(interaction)

    @discord.ui.button(label="Request Time Fix", style=discord.ButtonStyle.secondary,
                        emoji="📝", custom_id="flpd:clock:fix")
    async def request_fix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self.cog
        if not cog:
            return
        await interaction.response.send_modal(TimeFixModal(cog))

    @discord.ui.button(label="Leaderboard", style=discord.ButtonStyle.secondary,
                        emoji="🏆", custom_id="flpd:clock:leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_leaderboard(interaction)


class LeaderboardView(SafeView):
    """Ephemeral view shown after pressing the panel's Leaderboard button - lets the viewer
    toggle between 'since last reset' and 'all-time' without re-running a command."""

    def __init__(self, cog: "Clock", guild_id: int, all_time: bool = False) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.all_time = all_time
        self._sync_button_label()

    def _sync_button_label(self) -> None:
        self.toggle.label = "Show All-Time" if not self.all_time else "Show Since Last Reset"

    @discord.ui.button(label="Show All-Time", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.all_time = not self.all_time
        self._sync_button_label()
        embed = await self.cog.build_leaderboard_embed(self.guild_id, self.all_time)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]


class Clock(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]

    # ---------------- button handlers ----------------

    async def handle_clock_in(self, interaction: discord.Interaction) -> None:
        guild_id, user_id = interaction.guild_id, interaction.user.id
        existing = await self.db.open_shift(guild_id, user_id)
        if existing:
            raise BotError("You're already clocked in.")

        await self.db.start_shift(guild_id, user_id)

        config = await self.db.config(guild_id)
        if config["onduty_role"]:
            role = interaction.guild.get_role(config["onduty_role"])
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Clocked in")
                except discord.HTTPException:
                    pass

        embed = build_embed(config, title="🟢 Clocked In",
                             description=f"{interaction.user.mention} started a shift.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await send_log(self.bot, config["clock_log_channel"], build_embed(
            config, title="Officer Clocked In",
            description=f"{interaction.user.mention} clocked in.", color=0x2ECC71))

    async def handle_clock_out(self, interaction: discord.Interaction) -> None:
        guild_id, user_id = interaction.guild_id, interaction.user.id
        shift = await self.db.open_shift(guild_id, user_id)
        if not shift:
            raise BotError("You're not currently clocked in.")

        duration = await self.db.end_shift(shift["id"])

        config = await self.db.config(guild_id)
        if config["onduty_role"]:
            role = interaction.guild.get_role(config["onduty_role"])
            if role:
                try:
                    await interaction.user.remove_roles(role, reason="Clocked out")
                except discord.HTTPException:
                    pass

        embed = build_embed(config, title="🔴 Clocked Out",
                             description=f"Session length: **{fmt_duration(duration)}**")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await send_log(self.bot, config["clock_log_channel"], build_embed(
            config, title="Officer Clocked Out",
            description=f"{interaction.user.mention} clocked out after **{fmt_duration(duration)}**.",
            color=0xE74C3C))

    async def handle_my_time(self, interaction: discord.Interaction) -> None:
        guild_id, user_id = interaction.guild_id, interaction.user.id
        config = await self.db.config(guild_id)

        active = await self.db.open_shift(guild_id, user_id)
        active_line = "Not currently clocked in."
        if active:
            elapsed = self._elapsed(active["start_ts"])
            active_line = f"🟢 On duty for **{fmt_duration(elapsed)}**"

        cycle_secs, cycle_adj = await self.db.totals(guild_id, user_id, config["reset_ts"] or None)
        all_secs, all_adj = await self.db.totals(guild_id, user_id, None)

        embed = build_embed(config, title="⏱️ Your Time")
        embed.description = active_line
        embed.add_field(name="Since Last Reset",
                         value=fmt_duration(cycle_secs + cycle_adj), inline=True)
        embed.add_field(name="All-Time Total",
                         value=fmt_duration(all_secs + all_adj), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _elapsed(self, start_ts: int) -> int:
        import time
        return int(time.time()) - int(start_ts)

    async def handle_leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=await self.build_leaderboard_embed(interaction.guild_id, all_time=False),
            view=LeaderboardView(self, interaction.guild_id),
            ephemeral=True,
        )

    async def build_leaderboard_embed(self, guild_id: int, all_time: bool) -> discord.Embed:
        config = await self.db.config(guild_id)
        since = None if all_time else (config["reset_ts"] or None)
        rows = await self.db.leaderboard(guild_id, since)

        embed = build_embed(config, title="🏆 FLPD Hours Leaderboard")
        if not rows:
            embed.description = "No hours logged yet."
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = []
            for i, (user_id, secs) in enumerate(rows[:10], start=1):
                prefix = medals.get(i, f"**{i}.**")
                lines.append(f"{prefix} <@{user_id}> - {fmt_duration(secs)}")
            embed.description = "\n".join(lines)
        embed.set_footer(text="All-time" if all_time else "Since last reset")
        return embed

    # ---------------- time-fix workflow ----------------

    async def submit_timefix(self, interaction: discord.Interaction, minutes: int, reason: str) -> None:
        guild_id = interaction.guild_id
        config = await self.db.config(guild_id)
        if not config["timefix_channel"]:
            raise BotError(
                "Time-fix review channel isn't configured yet. Ask an admin to run "
                "`/config channel timefix`.")

        req_id = await self.db.create_timefix(guild_id, interaction.user.id, minutes, reason)

        channel = self.bot.get_channel(config["timefix_channel"])
        if channel is None:
            raise BotError("The configured time-fix channel no longer exists.")

        sign = "+" if minutes >= 0 else "-"
        embed = build_embed(
            config, title="📝 Time Fix Request",
            description=f"Requested by {interaction.user.mention}",
            color=0xF1C40F,
        )
        embed.add_field(name="Amount", value=f"{sign}{fmt_duration(abs(minutes) * 60)}", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Request #{req_id}")

        msg = await channel.send(
            content=f"<@&{config['timefix_ping_role']}>" if config["timefix_ping_role"] else None,
            embed=embed, view=TimeFixReviewView(self, req_id),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        await self.db.attach_timefix_message(req_id, channel.id, msg.id)

        await interaction.response.send_message(
            "Your time-fix request was submitted for review.", ephemeral=True)

    async def resolve_timefix(self, interaction: discord.Interaction, request_id: int, approve: bool) -> None:
        req = await self.db.get_timefix(request_id)
        if req is None:
            raise BotError("This request no longer exists.")
        if req["status"] != "pending":
            await interaction.response.send_message(
                f"This request was already **{req['status']}**.", ephemeral=True)
            return

        status = "approved" if approve else "denied"
        await self.db.resolve_timefix(request_id, status, interaction.user.id)

        config = await self.db.config(interaction.guild_id)
        if approve:
            await self.db.add_adjustment(
                interaction.guild_id, req["user_id"], req["minutes"], req["reason"],
                interaction.user.id)

        # Update the original message: disable buttons, show outcome.
        color = 0x2ECC71 if approve else 0xE74C3C
        embed = interaction.message.embeds[0]
        embed.color = discord.Color(color)
        embed.add_field(
            name="Reviewed by", value=f"{interaction.user.mention} - **{status.upper()}**",
            inline=False)
        new_view = discord.ui.View()  # empty view replaces the Approve/Deny buttons once resolved
        await interaction.response.edit_message(embed=embed, view=new_view)

        member = interaction.guild.get_member(req["user_id"])
        sign = "+" if req["minutes"] >= 0 else "-"
        result_embed = build_embed(
            config, title=f"Time Fix {status.capitalize()}",
            description=f"Your request for **{sign}{fmt_duration(abs(req['minutes']) * 60)}** "
                        f"was **{status}** by {interaction.user.mention}.",
            color=color)
        if req["reason"]:
            result_embed.add_field(name="Reason Given", value=req["reason"], inline=False)
        if member:
            await dm_safe(member, result_embed)

    # ---------------- slash commands ----------------

    @app_commands.command(name="clockpanel", description="Post the FLPD clock-in panel in this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clockpanel(self, interaction: discord.Interaction) -> None:
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(
            config, title="Clock into FLPD Patrol",
            description=(
                "In order to clock in press the button called **`Clock In`** below. To clock out press the button called **`Clock Out`**. If you'd like to check your time press the **`My Time`** button. **IF YOU NEED TO GET A TIME FIX THEN PRESS THE** `Request Time Fix` **BUTTON AND FILL OUT THE FIELDS AS NECESSARY**. To view global time press the **`Leaderborad`** button."
            ),
        )
        msg = await interaction.channel.send(embed=embed, view=ClockPanelView(self.bot))
        await self.db.save_panel(interaction.guild_id, interaction.channel_id, msg.id)
        await interaction.response.send_message("Panel posted.", ephemeral=True)

    @app_commands.command(name="hours", description="Check an officer's clocked hours.")
    @app_commands.describe(officer="Defaults to yourself.")
    async def hours(self, interaction: discord.Interaction,
                     officer: Optional[discord.Member] = None) -> None:
        target = officer or interaction.user
        if officer and officer.id != interaction.user.id:
            if not is_supervisor(interaction.user):  # type: ignore[arg-type]
                raise BotError("You can only check your own hours.")

        config = await self.db.config(interaction.guild_id)
        cycle_secs, cycle_adj = await self.db.totals(interaction.guild_id, target.id, config["reset_ts"] or None)
        all_secs, all_adj = await self.db.totals(interaction.guild_id, target.id, None)
        active = await self.db.open_shift(interaction.guild_id, target.id)

        embed = build_embed(config, title=f"⏱️ Hours - {target.display_name}")
        if active:
            embed.description = f"🟢 Currently on duty (since {self._elapsed(active['start_ts'])}s ago)"
        embed.add_field(name="Since Last Reset", value=fmt_duration(cycle_secs + cycle_adj))
        embed.add_field(name="All-Time", value=fmt_duration(all_secs + all_adj))
        await interaction.response.send_message(embed=embed, ephemeral=officer is None)

    @app_commands.command(name="hours_clear", description="Reset everyone's 'since last reset' hour counter (monthly reset).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def hours_clear(self, interaction: discord.Interaction) -> None:
        await self.db.reset_clock(interaction.guild_id)
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(
            config, title="Hours Reset",
            description=f"{interaction.user.mention} reset the monthly hour counter. "
                        "All-time totals are unaffected.",
            color=0xF39C12)
        await interaction.response.send_message(embed=embed)
        await send_log(self.bot, config["clock_log_channel"], embed)

    @app_commands.command(name="hours_adjust", description="Manually add or remove time for an officer.")
    @app_commands.describe(officer="The officer to adjust.", amount="e.g. 30m, 2h, -1h",
                            reason="Why you're adjusting their time.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def hours_adjust(self, interaction: discord.Interaction, officer: discord.Member,
                            amount: str, reason: str) -> None:
        negative = amount.strip().startswith("-")
        seconds = parse_duration(amount.strip().lstrip("-"))
        minutes = -(seconds // 60) if negative else seconds // 60
        await self.db.add_adjustment(interaction.guild_id, officer.id, minutes, reason, interaction.user.id)

        config = await self.db.config(interaction.guild_id)
        sign = "+" if minutes >= 0 else "-"
        embed = build_embed(
            config, title="Manual Time Adjustment",
            description=f"{interaction.user.mention} adjusted {officer.mention}'s time by "
                        f"**{sign}{fmt_duration(abs(minutes) * 60)}**.",
            color=0x3498DB)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await send_log(self.bot, config["clock_log_channel"], embed)

    @app_commands.command(name="leaderboard", description="Top officers by hours worked.")
    @app_commands.describe(all_time="Show all-time totals instead of since the last reset.")
    async def leaderboard(self, interaction: discord.Interaction, all_time: bool = False) -> None:
        config = await self.db.config(interaction.guild_id)
        since = None if all_time else (config["reset_ts"] or None)
        rows = await self.db.leaderboard(interaction.guild_id, since)

        embed = build_embed(config, title="🏆 FLPD Hours Leaderboard")
        if not rows:
            embed.description = "No hours logged yet."
        else:
            lines = []
            for i, (user_id, secs) in enumerate(rows[:10], start=1):
                lines.append(f"**{i}.** <@{user_id}> - {fmt_duration(secs)}")
            embed.description = "\n".join(lines)
        embed.set_footer(text="All-time" if all_time else "Since last reset")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clock(bot))