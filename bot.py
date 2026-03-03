import discord
from discord import app_commands
import re
import os
import json
from describe import get_image_title

# Load config from environment variables
TOKEN = os.environ["DISCORD_TOKEN"]

# MONITORED_GUILDS: JSON string mapping guild IDs to lists of channel IDs
# Example: '{"1234567890": [9876543210, 9876543211]}'
_raw = json.loads(os.environ["MONITORED_GUILDS"])
MONITORED_GUILDS = {int(k): [int(c) for c in v] for k, v in _raw.items()}

BOOSTER_REQUIRED_ROLE_ID = int(os.environ.get("BOOSTER_REQUIRED_ROLE_ID", "0"))

# Initialize bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True


class RoleManagerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


client = RoleManagerBot()

# URL regex pattern
url_pattern = re.compile(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)


@client.event
async def on_ready():
    print(f"Bot is ready and logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if (
        message.guild.id in MONITORED_GUILDS
        and message.channel.id in MONITORED_GUILDS[message.guild.id]
    ):
        has_media = len(message.attachments) > 0
        has_url = bool(url_pattern.search(message.content))

        if not (has_media or has_url):
            await message.delete()
            try:
                await message.channel.send(
                    content=f"{message.author.mention} Your message was deleted. Messages in this channel must include media or a URL.",
                    delete_after=10,
                )
            except discord.errors.Forbidden:
                print(f"Failed to send notification in channel {message.channel.id}")
        else:
            try:
                await message.add_reaction("🧵")
            except discord.errors.Forbidden:
                print(f"Failed to add reaction in channel {message.channel.id}")


@client.event
async def on_message_edit(before, after):
    if after.author == client.user:
        return

    if (
        after.guild.id in MONITORED_GUILDS
        and after.channel.id in MONITORED_GUILDS[after.guild.id]
    ):
        has_media = len(after.attachments) > 0
        has_url = bool(url_pattern.search(after.content))

        if not (has_media or has_url):
            await after.delete()
            try:
                await after.channel.send(
                    content=f"{after.author.mention} Your edited message was deleted. Messages in this channel must include media or a URL.",
                    delete_after=10,
                )
            except discord.errors.Forbidden:
                print(f"Failed to send notification in channel {after.channel.id}")


@client.event
async def on_raw_message_delete(payload):
    if payload.guild_id is None:
        return
    if (
        payload.guild_id not in MONITORED_GUILDS
        or payload.channel_id not in MONITORED_GUILDS[payload.guild_id]
    ):
        return

    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return

    thread_pattern = f"({payload.message_id})"

    # Fetch active threads via API instead of relying on cache
    try:
        active_threads = await guild.active_threads()
    except discord.errors.Forbidden:
        return

    for thread in active_threads:
        if thread.parent_id == payload.channel_id and thread_pattern in thread.name:
            try:
                await thread.delete()
                print(f"Deleted thread {thread.name} as the original message was deleted")
            except discord.errors.Forbidden:
                print(f"Failed to delete thread {thread.name}")
            break


@client.event
async def on_raw_reaction_add(payload):
    # Ignore reactions from the bot itself
    if payload.user_id == client.user.id:
        return

    if str(payload.emoji) != "🧵":
        return

    # Check if message is in a monitored channel
    if (
        payload.guild_id not in MONITORED_GUILDS
        or payload.channel_id not in MONITORED_GUILDS[payload.guild_id]
    ):
        return

    channel = client.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.errors.NotFound, discord.errors.Forbidden):
        return

    thread_name = None

    if message.attachments:
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                try:
                    image_title = get_image_title(attachment.url)
                    thread_name = f"{image_title} ({message.id})"
                except Exception as e:
                    print(f"Failed to generate image title: {e}")
                break

    if thread_name is None:
        thread_name = f"{message.author.display_name} ({message.id})"

    await message.create_thread(name=thread_name, auto_archive_duration=60)
    await message.clear_reaction("🧵")


@client.event
async def on_error(event, *args, **kwargs):
    print(f"Error in {event}:", exc_info=True)


if __name__ == "__main__":
    client.run(TOKEN)
