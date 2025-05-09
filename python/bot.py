import discord
from discord import app_commands
import re
import config

# Initialize bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True


class RoleManagerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync commands with Discord
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
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Check if message is in a monitored channel
    if (
        message.guild.id in config.MONITORED_GUILDS
        and message.channel.id in config.MONITORED_GUILDS[message.guild.id]
    ):
        # Check if message has attachments or URLs
        has_media = len(message.attachments) > 0
        has_url = bool(url_pattern.search(message.content))

        if not (has_media or has_url):
            # Delete the message
            await message.delete()
            # Send ephemeral notification
            try:
                await message.channel.send(
                    content=f"{message.author.mention} Your message was deleted. Messages in this channel must include media or a URL.",
                    delete_after=10,
                )
            except discord.errors.Forbidden:
                print(f"Failed to send notification in channel {message.channel.id}")
        else:
            # Valid message with media or URL - create a thread
            # Create a meaningful thread name
            thread_name = f"{message.author.display_name} ({message.id})"

            # Create the thread
            thread = await message.create_thread(
                name=thread_name, auto_archive_duration=60
            )
            # Remove the message author from the thread
            await thread.remove_user(message.author)


@client.event
async def on_message_edit(before, after):
    # Ignore edits from the bot itself
    if after.author == client.user:
        return

    # Check if edited message is in a monitored channel
    if (
        after.guild.id in config.MONITORED_GUILDS
        and after.channel.id in config.MONITORED_GUILDS[after.guild.id]
    ):
        # Check if the edited message has attachments or URLs
        has_media = len(after.attachments) > 0
        has_url = bool(url_pattern.search(after.content))

        if not (has_media or has_url):
            # Delete the message that no longer has media
            await after.delete()
            # Send ephemeral notification
            try:
                await after.channel.send(
                    content=f"{after.author.mention} Your edited message was deleted. Messages in this channel must include media or a URL.",
                    delete_after=10,
                )
            except discord.errors.Forbidden:
                print(f"Failed to send notification in channel {after.channel.id}")


@client.event
async def on_message_delete(message):
    # Ignore deletions from the bot itself
    if message.author == client.user:
        return

    # Check if deleted message was in a monitored channel
    if (
        message.guild.id in config.MONITORED_GUILDS
        and message.channel.id in config.MONITORED_GUILDS[message.guild.id]
    ):
        # Pattern to match in thread names
        thread_pattern = f"({message.id})"

        # Check all active threads in the channel
        for thread in message.channel.threads:
            # If thread name contains the message ID, it was created from this message
            if thread_pattern in thread.name:
                try:
                    await thread.delete()
                    print(
                        f"Deleted thread {thread.name} as the original message was deleted"
                    )
                except discord.errors.Forbidden:
                    print(f"Failed to delete thread {thread.name}")
                break


# Error handling
@client.event
async def on_error(event, *args, **kwargs):
    print(f"Error in {event}:", exc_info=True)


if __name__ == "__main__":
    client.run(config.TOKEN)
