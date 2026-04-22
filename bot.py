import discord
from discord import app_commands
from discord.ext import tasks
import re
import os
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
import asyncio
from describe import get_image_title

TOKEN = os.environ["DISCORD_TOKEN"]

_raw = json.loads(os.environ["MONITORED_GUILDS"])
MONITORED_GUILDS = {int(k): [int(c) for c in v] for k, v in _raw.items()}

BOOSTER_REQUIRED_ROLE_ID = int(os.environ.get("BOOSTER_REQUIRED_ROLE_ID", "0"))
MODERATOR_ROLE_ID = int(os.environ.get("MODERATOR_ROLE_ID", "0"))
# Role that sits directly above all custom booster roles — used for ordering
BOOSTER_ROLE_ANCHOR_ID = int(os.environ.get("BOOSTER_ROLE_ANCHOR_ID", "0"))
VOICE_TEXT_CHANNELS = [int(c) for c in json.loads(os.environ.get("VOICE_TEXT_CHANNELS", "[]"))]
BOOSTER_ROLES_FILE = os.environ.get("BOOSTER_ROLES_FILE", "/app/data/booster_roles.json")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # privileged intent — must be enabled in the Developer Portal


class RoleManagerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"Synced {len(synced)} global commands: {[c.name for c in synced]}")


client = RoleManagerBot()

url_pattern = re.compile(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
)


# ---------------------------------------------------------------------------
# Booster role persistence
# ---------------------------------------------------------------------------

def load_booster_roles() -> dict:
    if os.path.exists(BOOSTER_ROLES_FILE):
        with open(BOOSTER_ROLES_FILE) as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    return {}


def save_booster_roles(mapping: dict):
    directory = os.path.dirname(BOOSTER_ROLES_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(BOOSTER_ROLES_FILE, "w") as f:
        json.dump({str(k): str(v) for k, v in mapping.items()}, f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_color(color_str: str) -> Optional[discord.Color]:
    color_str = color_str.strip().lstrip("#")
    if len(color_str) != 6:
        return None
    try:
        r = int(color_str[0:2], 16)
        g = int(color_str[2:4], 16)
        b = int(color_str[4:6], 16)
        return discord.Color.from_rgb(r, g, b)
    except ValueError:
        return None


def member_is_booster(member: discord.Member) -> bool:
    if member.premium_since is not None:
        return True
    if BOOSTER_REQUIRED_ROLE_ID and any(r.id == BOOSTER_REQUIRED_ROLE_ID for r in member.roles):
        return True
    return False


def member_is_moderator(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if MODERATOR_ROLE_ID and any(r.id == MODERATOR_ROLE_ID for r in member.roles):
        return True
    return False


async def reorder_booster_roles(guild: discord.Guild, booster_roles: dict):
    """Position custom booster roles below the anchor role, oldest booster highest."""
    if not BOOSTER_ROLE_ANCHOR_ID:
        return
    anchor = guild.get_role(BOOSTER_ROLE_ANCHOR_ID)
    if anchor is None:
        return

    entries = []
    for user_id, role_id in booster_roles.items():
        role = guild.get_role(role_id)
        if role is None:
            continue
        member = guild.get_member(user_id)
        # Former boosters whose role is saved go below current boosters
        sort_key = (
            member.premium_since
            if member and member.premium_since
            else datetime.max.replace(tzinfo=timezone.utc)
        )
        entries.append((sort_key, role))

    # Oldest premium_since → highest position (just below anchor)
    entries.sort(key=lambda x: x[0])

    positions = {}
    anchor_pos = anchor.position
    for i, (_, role) in enumerate(entries):
        positions[role] = anchor_pos - 1 - i

    if positions:
        try:
            await guild.edit_role_positions(positions=positions)
        except discord.errors.Forbidden:
            print("Failed to reorder booster roles: missing permissions")
        except Exception as e:
            print(f"Failed to reorder booster roles: {e}")


# ---------------------------------------------------------------------------
# Voice channel text cleanup
# ---------------------------------------------------------------------------

async def _bulk_delete(channel_id: int, ids: list):
    """Delete a batch of message IDs using the bulk-delete endpoint (2–100 messages, <14 days old)."""
    if len(ids) == 1:
        await client.http.delete_message(channel_id, ids[0])
    else:
        await client.http.delete_messages(channel_id, ids)
    await asyncio.sleep(1)


async def _run_voice_cleanup():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    bulk_cutoff = now - timedelta(days=14)  # Discord rejects bulk-delete for messages older than this
    cutoff_obj = discord.Object(id=discord.utils.time_snowflake(cutoff))

    print(f"[cleanup] Starting voice channel cleanup ({len(VOICE_TEXT_CHANNELS)} channel(s))")

    for channel_id in VOICE_TEXT_CHANNELS:
        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"[cleanup] Channel {channel_id} not found in cache — skipping")
            continue

        print(f"[cleanup] Scanning #{channel.name} ({channel_id})")
        bulk_ids = []
        bulk_count = 0
        old_count = 0
        scan_count = 0

        try:
            async for message in channel.history(limit=None, before=cutoff_obj):
                scan_count += 1
                if scan_count % 500 == 0:
                    print(f"[cleanup] #{channel.name}: scanned {scan_count} messages ({bulk_count} bulk-deleted, {old_count} individually deleted so far)")

                if message.created_at >= bulk_cutoff:
                    bulk_ids.append(message.id)
                    if len(bulk_ids) == 100:
                        await _bulk_delete(channel_id, bulk_ids)
                        bulk_count += len(bulk_ids)
                        print(f"[cleanup] #{channel.name}: bulk-deleted batch of 100 (total so far: {bulk_count})")
                        bulk_ids = []
                else:
                    if bulk_ids:
                        await _bulk_delete(channel_id, bulk_ids)
                        bulk_count += len(bulk_ids)
                        print(f"[cleanup] #{channel.name}: bulk-deleted final batch of {len(bulk_ids)} (total: {bulk_count})")
                        bulk_ids = []
                    try:
                        await message.delete()
                        old_count += 1
                        if old_count % 50 == 0:
                            print(f"[cleanup] #{channel.name}: individually deleted {old_count} old messages so far")
                        await asyncio.sleep(1)
                    except discord.errors.NotFound:
                        pass
                    except discord.errors.Forbidden:
                        print(f"[cleanup] #{channel.name}: no permission to delete messages — aborting channel")
                        break

            if bulk_ids:
                await _bulk_delete(channel_id, bulk_ids)
                bulk_count += len(bulk_ids)
                print(f"[cleanup] #{channel.name}: bulk-deleted final batch of {len(bulk_ids)}")

        except discord.errors.Forbidden:
            print(f"[cleanup] #{channel.name}: no permission to read history — skipping")
            continue
        except Exception as e:
            print(f"[cleanup] #{channel.name}: unexpected error — {e!r}")
            continue

        total = bulk_count + old_count
        if total:
            print(f"[cleanup] #{channel.name}: done — {bulk_count} bulk-deleted, {old_count} individually deleted, {total} total")
        else:
            print(f"[cleanup] #{channel.name}: nothing to delete")

    print("[cleanup] Voice channel cleanup complete")


@tasks.loop(hours=1)
async def cleanup_voice_channels():
    await _run_voice_cleanup()


@cleanup_voice_channels.error
async def cleanup_error(error: Exception):
    print(f"[cleanup] Task crashed with unhandled error: {error!r}")


@cleanup_voice_channels.before_loop
async def before_cleanup():
    # Wait 30 seconds after startup before the first run so the bot is fully
    # settled and not competing with early interaction handling.
    await asyncio.sleep(30)
    print("[cleanup] Starting first cleanup run after startup delay")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    print(f"Bot is ready and logged in as {client.user}")
    print(f"VOICE_TEXT_CHANNELS config: {VOICE_TEXT_CHANNELS}")

    # Clear stale guild-specific commands left over from any previous version
    for guild in client.guilds:
        try:
            client.tree.clear_commands(guild=guild)
            await client.tree.sync(guild=guild)
            print(f"Cleared guild commands for {guild.name} ({guild.id})")
        except Exception as e:
            print(f"Failed to clear commands for {guild.name}: {e!r}")

    if not VOICE_TEXT_CHANNELS:
        print("[cleanup] VOICE_TEXT_CHANNELS is empty — cleanup task not started")
    elif cleanup_voice_channels.is_running():
        print("[cleanup] Cleanup task already running")
    else:
        print("[cleanup] Starting cleanup task (first run in 30s)")
        cleanup_voice_channels.start()


@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Member stopped boosting — remove the role but keep it saved for later
    if before.premium_since is not None and after.premium_since is None:
        booster_roles = load_booster_roles()
        role_id = booster_roles.get(after.id)
        if role_id:
            role = after.guild.get_role(role_id)
            if role and role in after.roles:
                try:
                    await after.remove_roles(role, reason="Member stopped boosting")
                    print(f"Removed booster role from {after.display_name} — role saved for future use")
                except discord.errors.Forbidden:
                    print(f"Failed to remove booster role from {after.display_name}")


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
    if payload.user_id == client.user.id:
        return

    if str(payload.emoji) != "🧵":
        return

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


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

async def _role_target(interaction: discord.Interaction) -> Optional[discord.Member]:
    """Return the member whose role the autocomplete should reflect."""
    namespace_user = getattr(interaction.namespace, "user", None)
    if namespace_user and member_is_moderator(interaction.user):
        return namespace_user
    return interaction.user


async def autocomplete_role_name(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    target = await _role_target(interaction)
    booster_roles = load_booster_roles()
    role_id = booster_roles.get(target.id)
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role and (not current or current.lower() in role.name.lower()):
            return [app_commands.Choice(name=role.name, value=role.name)]
    return []


async def autocomplete_role_color(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    target = await _role_target(interaction)
    booster_roles = load_booster_roles()
    role_id = booster_roles.get(target.id)
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role and role.color.value != 0:
            hex_color = f"#{role.color.value:06X}"
            if not current or current.lower() in hex_color.lower():
                return [app_commands.Choice(name=hex_color, value=hex_color)]
    return []


@client.tree.command(name="role", description="Set your custom booster role name and colour")
@app_commands.describe(
    name="The name for your custom role (leave blank to keep current)",
    color="Role colour in hex format e.g. #FF0000 (leave blank to keep current)",
    user="[Moderators only] The user whose role to set",
)
@app_commands.autocomplete(name=autocomplete_role_name, color=autocomplete_role_color)
async def set_role(
    interaction: discord.Interaction,
    name: Optional[str] = None,
    color: Optional[str] = None,
    user: Optional[discord.Member] = None,
):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return  # Interaction token expired before we could acknowledge it

    guild = interaction.guild
    caller = interaction.user

    if user is not None:
        if not member_is_moderator(caller):
            await interaction.followup.send(
                "You don't have permission to set roles for others.", ephemeral=True
            )
            return
        target = user
    else:
        target = caller

    if not member_is_booster(target):
        msg = (
            "You must be a server booster to use this command."
            if target == caller
            else f"{target.display_name} is not a server booster."
        )
        await interaction.followup.send(msg, ephemeral=True)
        return

    color_parsed: Optional[discord.Color] = None
    if color is not None:
        color_parsed = parse_color(color)
        if color_parsed is None:
            await interaction.followup.send(
                "Invalid color. Use hex format like `#FF0000` or `FF0000`.", ephemeral=True
            )
            return

    if name is not None and len(name) > 100:
        await interaction.followup.send("Role name must be 100 characters or less.", ephemeral=True)
        return

    booster_roles = load_booster_roles()
    existing_role_id = booster_roles.get(target.id)
    created = False

    if existing_role_id:
        role = guild.get_role(existing_role_id)
        if role:
            final_name = name if name is not None else role.name
            final_color = color_parsed if color_parsed is not None else role.color
            if name is not None or color is not None:
                await role.edit(name=final_name, color=final_color)
        else:
            # Role was deleted externally — need both fields to recreate
            if name is None or color is None:
                await interaction.followup.send(
                    "Your previous role no longer exists. Please provide both a name and colour to create a new one.",
                    ephemeral=True,
                )
                return
            role = await guild.create_role(name=name, color=color_parsed)
            booster_roles[target.id] = role.id
            save_booster_roles(booster_roles)
            created = True
    else:
        if name is None or color is None:
            await interaction.followup.send(
                "You don't have a custom role yet. Please provide both a name and colour.",
                ephemeral=True,
            )
            return
        role = await guild.create_role(name=name, color=color_parsed)
        booster_roles[target.id] = role.id
        save_booster_roles(booster_roles)
        created = True

    if role not in target.roles:
        await target.add_roles(role)

    await reorder_booster_roles(guild, booster_roles)

    if created:
        msg = f"Custom role **{role.name}** has been created!"
    elif name is None and color is None:
        msg = f"Your role **{role.name}** is unchanged."
    else:
        msg = f"Custom role **{role.name}** has been updated!"

    await interaction.followup.send(msg, ephemeral=True)


@client.tree.command(
    name="importrole",
    description="[Admin] Link an existing role to a user as their booster role",
)
@app_commands.describe(
    user="The user to assign the role to",
    role="The existing role to import",
)
async def import_role(
    interaction: discord.Interaction,
    user: discord.Member,
    role: discord.Role,
):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return  # Interaction token expired before we could acknowledge it

    if not member_is_moderator(interaction.user):
        await interaction.followup.send(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    booster_roles = load_booster_roles()
    existing_role_id = booster_roles.get(user.id)

    if existing_role_id and existing_role_id != role.id:
        existing = interaction.guild.get_role(existing_role_id)
        existing_name = existing.name if existing else f"ID {existing_role_id}"
        await interaction.followup.send(
            f"{user.display_name} already has a booster role (**{existing_name}**). "
            f"Use `/role` to update it, or ask an admin to reassign.",
            ephemeral=True,
        )
        return

    booster_roles[user.id] = role.id
    save_booster_roles(booster_roles)

    if role not in user.roles:
        await user.add_roles(role)

    await reorder_booster_roles(interaction.guild, booster_roles)

    await interaction.followup.send(
        f"Role **{role.name}** imported and assigned to {user.display_name}.",
        ephemeral=True,
    )


if __name__ == "__main__":
    client.run(TOKEN)
