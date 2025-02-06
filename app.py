import discord
import re
import config

# Initialize bot with necessary intents
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

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


# Error handling
@client.event
async def on_error(event, *args, **kwargs):
    print(f"Error in {event}:", exc_info=True)


if __name__ == "__main__":
    client.run(config.TOKEN)
