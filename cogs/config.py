"""Configurable embed system: colors/branding, log channels, and a manual embed builder."""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import BotError, build_embed, parse_color, SafeModal

CHANNEL_FIELDS = {
    "mod_log": "mod_log_channel",
    "discipline_log": "discipline_log_channel",
    "clock_log": "clock_log_channel",
    "timefix": "timefix_channel",
}


class EmbedBuilderModal(SafeModal, title="Build an Embed"):
    embed_title = discord.ui.TextInput(label="Title", max_length=256, required=False)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                        max_length=4000)
    color = discord.ui.TextInput(label="Color (hex, e.g. #0A2F5C)", required=False, max_length=20)
    image_url = discord.ui.TextInput(label="Image URL", required=False, max_length=500)

    def __init__(self, config, target_channel: discord.abc.Messageable) -> None:
        super().__init__()
        self.config = config
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color_val = parse_color(self.color.value) if self.color.value else None
        embed = build_embed(
            self.config, title=self.embed_title.value or None,
            description=self.description.value, color=color_val)
        if self.image_url.value:
            embed.set_image(url=self.image_url.value)

        await self.target_channel.send(embed=embed)
        await interaction.response.send_message(
            f"Embed posted in {self.target_channel.mention}.", ephemeral=True)


class Config(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # type: ignore[attr-defined]

    config_group = app_commands.Group(
        name="config", description="Configure FLPD bot branding and log channels.",
        default_permissions=discord.Permissions(manage_guild=True))

    # ---------------- embed branding ----------------

    @config_group.command(name="color", description="Set the default embed color for the bot.")
    @app_commands.describe(value="Hex code (e.g. #0A2F5C) or a named color like 'blue'.")
    async def set_color(self, interaction: discord.Interaction, value: str) -> None:
        color_int = parse_color(value)
        await self.db.set_config(interaction.guild_id, "embed_color", color_int)
        preview = build_embed(await self.db.config(interaction.guild_id),
                               title="Preview", description="This is your new embed color.")
        await interaction.response.send_message(embed=preview, ephemeral=True)

    @config_group.command(name="footer", description="Set the footer text shown on every embed.")
    async def set_footer(self, interaction: discord.Interaction, text: str) -> None:
        await self.db.set_config(interaction.guild_id, "embed_footer", text)
        await interaction.response.send_message(f"Footer set to: `{text}`", ephemeral=True)

    @config_group.command(name="icon", description="Set the small footer icon (a direct image URL).")
    async def set_icon(self, interaction: discord.Interaction, url: str) -> None:
        await self.db.set_config(interaction.guild_id, "embed_icon", url)
        await interaction.response.send_message("Footer icon updated.", ephemeral=True)

    @config_group.command(name="thumbnail", description="Set the thumbnail image shown on every embed.")
    async def set_thumbnail(self, interaction: discord.Interaction, url: str) -> None:
        await self.db.set_config(interaction.guild_id, "embed_thumbnail", url)
        await interaction.response.send_message("Thumbnail updated.", ephemeral=True)

    @config_group.command(name="show", description="Preview the current embed styling and channel setup.")
    async def show(self, interaction: discord.Interaction) -> None:
        config = await self.db.config(interaction.guild_id)
        embed = build_embed(config, title="Current FLPD Bot Configuration")

        def chan(cid: Optional[int]) -> str:
            return f"<#{cid}>" if cid else "*Not set*"

        embed.add_field(name="Mod Log", value=chan(config["mod_log_channel"]), inline=True)
        embed.add_field(name="Discipline Log", value=chan(config["discipline_log_channel"]), inline=True)
        embed.add_field(name="Clock Log", value=chan(config["clock_log_channel"]), inline=True)
        embed.add_field(name="Time-Fix Review", value=chan(config["timefix_channel"]), inline=True)
        embed.add_field(name="Time-Fix Ping Role",
                         value=f"<@&{config['timefix_ping_role']}>" if config["timefix_ping_role"] else "*None*",
                         inline=True)
        embed.add_field(name="On-Duty Role",
                         value=f"<@&{config['onduty_role']}>" if config["onduty_role"] else "*Not set*",
                         inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- log channels ----------------

    @config_group.command(name="channel", description="Set a log/review channel by pasting its ID.")
    @app_commands.describe(kind="Which channel to set.",
                            channel_id="Right-click the channel (Developer Mode on) -> Copy Channel ID.")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Moderation Log", value="mod_log"),
        app_commands.Choice(name="Discipline Log", value="discipline_log"),
        app_commands.Choice(name="Clock Log", value="clock_log"),
        app_commands.Choice(name="Time-Fix Review Queue", value="timefix"),
    ])
    async def set_channel(self, interaction: discord.Interaction,
                           kind: app_commands.Choice[str], channel_id: str) -> None:
        channel_id = channel_id.strip()
        if not channel_id.isdigit():
            raise BotError(
                "That doesn't look like a channel ID. Enable Developer Mode "
                "(User Settings -> Advanced), then right-click the channel -> Copy Channel ID.")

        cid = int(channel_id)
        channel = interaction.guild.get_channel(cid)
        if channel is None:
            raise BotError(
                "I can't find a channel with that ID in this server. Double-check the ID "
                "and make sure I have access to that channel.")
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise BotError("That ID belongs to something that isn't a text channel.")

        field = CHANNEL_FIELDS[kind.value]
        await self.db.set_config(interaction.guild_id, field, cid)
        await interaction.response.send_message(
            f"{kind.name} set to {channel.mention} (`{cid}`).", ephemeral=True)

    @config_group.command(name="timefix_role",
                           description="Role pinged in the review channel when a time-fix request comes in.")
    @app_commands.describe(role_id="Right-click the role (Developer Mode on) -> Copy Role ID. "
                                    "Leave blank to stop pinging.")
    async def set_timefix_role(self, interaction: discord.Interaction, role_id: Optional[str] = None) -> None:
        if role_id is None or not role_id.strip():
            await self.db.set_config(interaction.guild_id, "timefix_ping_role", None)
            await interaction.response.send_message(
                "Time-fix requests will no longer ping a role.", ephemeral=True)
            return

        role_id = role_id.strip()
        if not role_id.isdigit():
            raise BotError(
                "That doesn't look like a role ID. Enable Developer Mode, then right-click "
                "the role in Server Settings -> Roles, or @mention it and strip the <@&...> wrapper.")

        rid = int(role_id)
        role = interaction.guild.get_role(rid)
        if role is None:
            raise BotError("I can't find a role with that ID in this server.")

        await self.db.set_config(interaction.guild_id, "timefix_ping_role", rid)
        await interaction.response.send_message(
            f"Time-fix requests will now ping {role.mention}.", ephemeral=True)

    @config_group.command(name="onduty_role", description="Role automatically applied while clocked in.")
    async def set_onduty_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.db.set_config(interaction.guild_id, "onduty_role", role.id)
        await interaction.response.send_message(f"On-duty role set to {role.mention}.", ephemeral=True)

    # ---------------- manual embed builder ----------------

    @app_commands.command(name="embed", description="Build and post a custom embed using the bot's branding.")
    @app_commands.describe(channel="Where to post it (defaults to this channel).")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed(self, interaction: discord.Interaction,
                     channel: Optional[discord.TextChannel] = None) -> None:
        config = await self.db.config(interaction.guild_id)
        target = channel or interaction.channel
        await interaction.response.send_modal(EmbedBuilderModal(config, target))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Config(bot))