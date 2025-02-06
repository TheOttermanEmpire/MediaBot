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


# Add this to config.py:
# COMMAND_CHANNEL_ID = 123456789  # Replace with your command channel ID
# HORNY_ROLE_ID = 987654321      # Replace with your role ID


@client.tree.command(name="horny", description="Assigns the horny role to the user")
async def horny(interaction: discord.Interaction):
    # Check if command is used in the correct channel
    if interaction.channel_id != config.COMMAND_CHANNEL_ID:
        await interaction.response.send_message(
            "This command can only be used in the designated channel.", ephemeral=True
        )
        return

    try:
        # Get the role object
        role = interaction.guild.get_role(config.HORNY_ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "Role not found. Please contact an administrator.", ephemeral=True
            )
            return

        # Add the role to the user
        await interaction.user.add_roles(role)
        await interaction.response.send_message(
            f"Role {role.name} has been added.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to manage roles.", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"An error occurred: {str(e)}", ephemeral=True
        )


@client.tree.command(name="unhorny", description="Removes the horny role from the user")
async def unhorny(interaction: discord.Interaction):
    # Check if command is used in the correct channel
    if interaction.channel_id != config.COMMAND_CHANNEL_ID:
        await interaction.response.send_message(
            "This command can only be used in the designated channel.", ephemeral=True
        )
        return

    try:
        # Get the role object
        role = interaction.guild.get_role(config.HORNY_ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "Role not found. Please contact an administrator.", ephemeral=True
            )
            return

        # Remove the role from the user
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(
            f"Role {role.name} has been removed.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to manage roles.", ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"An error occurred: {str(e)}", ephemeral=True
        )


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
