# Copyright (c) 2025 Benoît Pelletier
# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import functools
from typing import Optional
import discord
from discord.ext import commands
from dismob import log, filehelper
from dismob.event import Event
import aiosqlite
import random
import datetime

async def setup(bot: commands.Bot):
    log.info("Module `welcome` setup")
    filehelper.ensure_directory("db")
    await bot.add_cog(Welcome(bot))

async def teardown(bot: commands.Bot):
    log.info("Module `welcome` teardown")
    await bot.remove_cog("Welcome")

class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.db_path: str = "db/welcome.db"
        self.db_ready: bool = False
        def template(interaction: discord.Interaction, greeted_member: discord.Member) -> None: pass
        self.on_greeting: Event = Event(template)
        self.bot.loop.create_task(self.init_db())
        self._active_join_messages = {}  # {guild_id: {member_id: (message, delete_task)}}

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS welcome_config (
                    guild_id INTEGER PRIMARY KEY,
                    join_enabled BOOLEAN DEFAULT 1,
                    leave_enabled BOOLEAN DEFAULT 1,
                    join_channel_id INTEGER,
                    leave_channel_id INTEGER,
                    join_title TEXT DEFAULT 'Welcome',
                    leave_title TEXT DEFAULT 'Goodbye',
                    join_duration INTEGER DEFAULT 0,   -- duration in seconds, 0 or negative means do not delete
                    leave_duration INTEGER DEFAULT 0   -- duration in seconds, 0 or negative means do not delete
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS welcome_join_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    message TEXT NOT NULL,
                    FOREIGN KEY (guild_id) REFERENCES welcome_config(guild_id) ON DELETE CASCADE
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS welcome_leave_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    message TEXT NOT NULL,
                    FOREIGN KEY (guild_id) REFERENCES welcome_config(guild_id) ON DELETE CASCADE
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS welcome_greet_counts (
                    guild_id INTEGER,
                    greeter_id INTEGER,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, greeter_id)
                )
            """)
            await db.commit()
            self.db_ready = True
            log.info("Welcome database initialized")

    async def increment_greet_count(self, guild_id: int, greeter_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO welcome_greet_counts (guild_id, greeter_id, count)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, greeter_id)
                DO UPDATE SET count = count + 1
            """, (guild_id, greeter_id))
            await db.commit()

    async def get_greet_count(self, guild_id: int, greeter_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT count FROM welcome_greet_counts
                WHERE guild_id = ? AND greeter_id = ?
            """, (guild_id, greeter_id)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def send_formatted_message(self, channel: discord.TextChannel, title: str, message: str, member: discord.Member, color: discord.Color, view = None, delete_after: Optional[int] = None) -> Optional[discord.Message]:
        """Send a formatted embed message to the specified channel.

        Args:
            channel: The channel to send the message to
            message: The message template to format
            member: The member that triggered the event
            color: The color of the embed
            delete_after: Duration in seconds to delete the message, or None/<=0 to not delete
        Returns:
            The sent message, or None if failed
        """
        try:
            data: dict = {
                "member": member.display_name,
                "server": member.guild.name,
                "mention": member.mention
            }

            embed = discord.Embed(
                title=message.format(**data).replace("\\n", "\n"),
                #description=message.format(**data).replace("\\n", "\n"),
                color=color
            )
            #embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            sent_msg: discord.Message = await channel.send(embed=embed, view=view)
            if delete_after and delete_after > 0:
                async def delete_later(msg: discord.Message, delay):
                    try:
                        await discord.utils.sleep_until(discord.utils.utcnow() + datetime.timedelta(seconds=delay))
                        await msg.delete()
                    except Exception as e:
                        log.error(f"Failed to auto-delete message: {e}")
                task = self.bot.loop.create_task(delete_later(sent_msg, delete_after))
                return sent_msg, task
            return sent_msg, None
        except Exception as e:
            log.error(f"Error sending formatted message: {e}")
            return None, None

    def db_ready_only(func):
        @functools.wraps(func)
        async def wrapper(self, ctx: discord.Interaction, *args, **kwargs):
            if not self.db_ready:
                await log.client(ctx, "Database is not ready yet. Please try again in a few seconds.")
                return
            return await func(self, ctx, *args, **kwargs)
        return wrapper

    #####                     #####
    #           events            #
    #####                     ##### 

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.db_ready:
            return
        channel = None
        enabled = False
        messages = []
        title = None
        join_duration = 0
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT join_channel_id, join_enabled, join_title, join_duration FROM welcome_config
                    WHERE guild_id = ?
                """, (member.guild.id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        if row[0] is not None:
                            channel = member.guild.get_channel(row[0])
                        if row[1] is not None:
                            enabled = row[1]
                        if row[2]:
                            title = row[2]
                        if row[3] is not None:
                            join_duration = row[3]

                if not enabled:
                    return

                if not channel:
                    log.warning(f"Join channel not found for guild {member.guild.id}. Please set it using the `/welcome join set` command.")
                    return

                # Get all join messages for this guild
                async with db.execute("""
                    SELECT message FROM welcome_join_messages
                    WHERE guild_id = ?
                """, (member.guild.id,)) as cursor:
                    messages = [r[0] async for r in cursor]
        except Exception as e:
            log.error(f"Database error in on_member_join: {e}")
            return

        if not messages:
            log.warning(f"No join messages configured for guild {member.guild.id}. Please set them using the `/welcome join add-message` command.")
            return

        message = random.choice(messages)

        view = discord.ui.View(timeout=None)
        view.add_item(WelcomeButton(self, member))
        sent_msg, delete_task = await self.send_formatted_message(channel, title, message, member, discord.Color.green(), view=view, delete_after=join_duration)
        # Track the join message for possible early deletion
        if sent_msg and join_duration and join_duration > 0:
            if member.guild.id not in self._active_join_messages:
                self._active_join_messages[member.guild.id] = {}
            self._active_join_messages[member.guild.id][member.id] = (sent_msg, delete_task)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self.db_ready:
            return
        # If the member had a join message pending deletion, delete it now
        guild_msgs = self._active_join_messages.get(member.guild.id)
        if guild_msgs and member.id in guild_msgs:
            msg, task = guild_msgs.pop(member.id)
            try:
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
                if task:
                    task.cancel()
            except Exception as e:
                log.error(f"Failed to delete join message for leaving member: {e}")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT leave_channel_id, leave_enabled, leave_title, leave_duration FROM welcome_config
                    WHERE guild_id = ?
                """, (member.guild.id,)) as cursor:
                    row = await cursor.fetchone()

                channel = None
                enabled = False
                title = None
                leave_duration = 0
                if row:
                    if row[0] is not None:
                        channel = member.guild.get_channel(row[0])
                    if row[1] is not None:
                        enabled = row[1]
                    if row[2]:
                        title = row[2]
                    if row[3] is not None:
                        leave_duration = row[3]

                if not enabled:
                    return

                if not channel:
                    log.warning(f"Leave channel not found for guild {member.guild.id}. Please set it using the `/welcome leave set` command.")
                    return

                # Get all leave messages for this guild
                async with db.execute("""
                    SELECT message FROM welcome_leave_messages
                    WHERE guild_id = ?
                """, (member.guild.id,)) as cursor:
                    messages = [r[0] async for r in cursor]

                if not messages:
                    log.warning(f"No leave messages configured for guild {member.guild.id}. Please set them using the `/welcome leave add-message` command.")
                    return

                message = random.choice(messages)
                await self.send_formatted_message(channel, title, message, member, discord.Color.red(), delete_after=leave_duration)
        except Exception as e:
            log.error(f"Database error in on_member_remove: {e}")

    #####                       #####
    #           commands            #
    #####                       ##### 

    @discord.app_commands.command(name="welcome-count", description="Check how many people you have welcomed")
    @db_ready_only
    async def greet_count(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        target = member or (interaction.user if hasattr(interaction, "user") else interaction.author)
        member_str: str = "You have" if interaction.user.id == target.id else f"{target.mention} has"

        count = await self.get_greet_count(interaction.guild.id, target.id)
        if count is None or count <= 0:
            await log.client(interaction, f"{member_str} not welcomed anyone in this server.")
        else:
            await log.client(interaction, f"{member_str} welcomed {count} member{'s' if count > 1 else ''} in this server.")

    # Create a slash command group for managing welcome settings
    welcomeGroup = discord.app_commands.Group(name="welcome", description="Manage welcome messages settings", default_permissions=discord.Permissions(manage_guild=True))

    # Create a slash command group for managing join messages settings
    joinGroup = discord.app_commands.Group(name="join", description="Manage join messages settings", parent=welcomeGroup)

    @joinGroup.command(name="settings", description="Set the config for join messages")
    @db_ready_only
    async def set_join_config(self, ctx: discord.Interaction, channel: Optional[discord.TextChannel] = None, title: Optional[str] = None, enable: Optional[bool] = None, duration: Optional[int] = None) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT join_channel_id, join_title, join_enabled, join_duration FROM welcome_config
                    WHERE guild_id = ?
                """, (ctx.guild.id,)) as cursor:
                    row = await cursor.fetchone()

                if channel is None and title is None and enable is None and duration is None:
                    if row:
                        channel_id, title, enabled, join_duration = row
                        channel_mention = f"<#{channel_id}>" if channel_id else "None"
                        await log.client(ctx, f"Current join config: Channel: {channel_mention}, Title: {title}, Enabled: {enabled}, Duration: {join_duration}s")
                    else:
                        await log.client(ctx, "No join config found for this server.")
                else:
                    if row:
                        old_channel_id, old_title, old_enabled, old_duration = row
                    else:
                        old_channel_id = None
                        old_title = "Welcome"
                        old_enabled = True
                        old_duration = 0

                    channel_id = channel.id if channel else old_channel_id
                    new_title = title if title is not None else old_title
                    new_enable = enable if enable is not None else old_enabled
                    new_duration = duration if duration is not None else old_duration
                    await db.execute("""
                        INSERT INTO welcome_config (guild_id, join_channel_id, join_title, join_enabled, join_duration)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET join_channel_id = ?, join_title = ?, join_enabled = ?, join_duration = ?
                    """, (ctx.guild.id, channel_id, new_title, new_enable, new_duration, channel_id, new_title, new_enable, new_duration))
                    await db.commit()
                    await log.success(ctx, f"Join message config updated.")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while updating the database. Please try again later.\n```\n{e}\n```")

    @joinGroup.command(name="add-message", description="Add a new join message")
    @db_ready_only
    async def add_join_message(self, ctx: discord.Interaction, message: str) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO welcome_join_messages (guild_id, message)
                    VALUES (?, ?)
                """, (ctx.guild.id, message))
                await db.commit()
                await log.success(ctx, f"Join message added: {message}")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while adding the join message. Please try again later.\n```\n{e}\n```")

    @joinGroup.command(name="remove-message", description="Remove a join message by ID")
    @db_ready_only
    async def remove_join_message(self, ctx: discord.Interaction, message_id: int) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute("""
                    DELETE FROM welcome_join_messages
                    WHERE id = ? AND guild_id = ?
                """, (message_id, ctx.guild.id))
                if result.rowcount > 0:
                    await db.commit()
                    await log.success(ctx, f"Join message with ID {message_id} removed.")
                else:
                    await log.failure(ctx, f"No join message found with ID {message_id}.")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while removing the join message. Please try again later.\n```\n{e}\n```")

    @joinGroup.command(name="list-message", description="List all join messages")
    @db_ready_only
    async def list_join_messages(self, ctx: discord.Interaction) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT id, message FROM welcome_join_messages
                    WHERE guild_id = ?
                """, (ctx.guild.id,)) as cursor:
                    messages = await cursor.fetchall()

            if not messages:
                await log.client(ctx, "No join messages configured for this server.")
                return

            message_list = "\n".join([f"{msg[0]}: {msg[1]}" for msg in messages])
            await log.client(ctx, f"Join messages:\n{message_list}")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while listing join messages. Please try again later.\n```\n{e}\n```")

    @joinGroup.command(name="test", description="Test the join message for yourself or another member")
    @db_ready_only
    async def test_join_message(self, ctx: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        target_member = member or (ctx.user if hasattr(ctx, "user") else ctx.author)
        await self.on_member_join(target_member)
        await log.client(ctx, f"Testing join message for member {target_member.mention}.")

    # Create a slash command group for managing leave messages settings
    leaveGroup = discord.app_commands.Group(name="leave", description="Manage leave messages settings", parent=welcomeGroup)

    @leaveGroup.command(name="settings", description="Set the config for leave messages")
    @db_ready_only
    async def set_leave_config(self, ctx: discord.Interaction, channel: Optional[discord.TextChannel] = None, title: Optional[str] = None, enable: Optional[bool] = None, duration: Optional[int] = None) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT leave_channel_id, leave_title, leave_enabled, leave_duration FROM welcome_config
                    WHERE guild_id = ?
                """, (ctx.guild.id,)) as cursor:
                    row = await cursor.fetchone()

                if channel is None and title is None and enable is None and duration is None:
                    if row:
                        channel_id, title, enabled, leave_duration = row
                        channel_mention = f"<#{channel_id}>" if channel_id else "None"
                        await log.client(ctx, f"Current leave config: Channel: {channel_mention}, Title: {title}, Enabled: {enabled}, Duration: {leave_duration}s")
                    else:
                        await log.client(ctx, "No leave config found for this server.")
                else:
                    # Get old values and update with new values
                    if row:
                        old_channel_id, old_title, old_enabled, old_duration = row
                    else:
                        old_channel_id = None
                        old_title = "Goodbye"
                        old_enabled = True
                        old_duration = 0

                    channel_id = channel.id if channel else old_channel_id
                    new_title = title if title is not None else old_title
                    new_enable = enable if enable is not None else old_enabled
                    new_duration = duration if duration is not None else old_duration

                    await db.execute("""
                        INSERT INTO welcome_config (guild_id, leave_channel_id, leave_title, leave_enabled, leave_duration)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET leave_channel_id = ?, leave_title = ?, leave_enabled = ?, leave_duration = ?
                    """, (ctx.guild.id, channel_id, new_title, new_enable, new_duration, channel_id, new_title, new_enable, new_duration))
                    await db.commit()
                    await log.success(ctx, f"Leave message config updated.")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while updating the database. Please try again later.\n```\n{e}\n```")

    @leaveGroup.command(name="add-message", description="Add a new leave message")
    @db_ready_only
    async def add_leave_message(self, ctx: discord.Interaction, message: str) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO welcome_leave_messages (guild_id, message)
                    VALUES (?, ?)
                """, (ctx.guild.id, message))
                await db.commit()
                await log.success(ctx, f"Leave message added: {message}")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while adding the leave message. Please try again later.\n```\n{e}\n```")

    @leaveGroup.command(name="remove-message", description="Remove a leave message by ID")
    @db_ready_only
    async def remove_leave_message(self, ctx: discord.Interaction, message_id: int) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                result = await db.execute("""
                    DELETE FROM welcome_leave_messages
                    WHERE id = ? AND guild_id = ?
                """, (message_id, ctx.guild.id))
                if result.rowcount > 0:
                    await db.commit()
                    await log.success(ctx, f"Leave message with ID {message_id} removed.")
                else:
                    await log.failure(ctx, f"No leave message found with ID {message_id}.")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while removing the leave message. Please try again later.\n```\n{e}\n```")

    @leaveGroup.command(name="list-message", description="List all leave messages")
    @db_ready_only
    async def list_leave_messages(self, ctx: discord.Interaction) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("""
                    SELECT id, message FROM welcome_leave_messages
                    WHERE guild_id = ?
                """, (ctx.guild.id,)) as cursor:
                    messages = await cursor.fetchall()

            if not messages:
                await log.client(ctx, "No leave messages configured for this server.")
                return

            message_list = "\n".join([f"{msg[0]}: {msg[1]}" for msg in messages])
            await log.client(ctx, f"Leave messages:\n{message_list}")
        except Exception as e:
            await log.failure(ctx, f"An error occurred while listing leave messages. Please try again later.\n```\n{e}\n```")

    @leaveGroup.command(name="test", description="Test the leave message for yourself or another member")
    @db_ready_only
    async def test_leave_message(self, ctx: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        target_member = member or (ctx.user if hasattr(ctx, "user") else ctx.author)
        await self.on_member_remove(target_member)
        await log.client(ctx, f"Testing leave message for member {target_member.mention}.")

class WelcomeButton(discord.ui.Button):
    def __init__(self, parent: Welcome, member: discord.Member):
        super().__init__(
            label="👋 Welcome",
            style=discord.ButtonStyle.secondary
        )
        self.parent = parent
        self.member = member
        self.greeters: list[int] = []

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id == self.member.id:
            await log.client(interaction, "You cannot welcome yourself!")
            return

        # Check if already greeted
        if interaction.user.id in self.greeters:
            await log.client(interaction, "You have already welcomed this member!")
            return

        await self.parent.increment_greet_count(interaction.guild.id, interaction.user.id)

        # Record the greeting
        self.greeters.append(interaction.user.id)
        
        # Update the button label
        self.label = f"👋 Welcomed ({len(self.greeters)})"
        await interaction.response.edit_message(view=self.view)

        # Dispatch the greeting event so that other modules can do some actions (eg. give xp)
        self.parent.on_greeting.dispatch(interaction, self.member)