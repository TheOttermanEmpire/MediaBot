"""VRChat group linking: self-service group invites and auto-removal on leave/ban.

This module registers no events of its own — bot.py owns the single
discord.Client and forwards guild_member_remove here via handle_member_remove,
since discord.py allows only one handler per event.

Ownership of a VRChat account is proven by *accepting* the group invite the
bot sends: /vrcjoin never records a link until the target account actually
shows up as a group member, which requires an action only the account owner
can take. /vrclinkadmin skips that proof and links immediately, so it's
restricted to admins who vouch for the pairing themselves.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp
import discord
import pyotp
from discord import app_commands

VRC_USERNAME = os.environ.get("VRC_USERNAME", "")
VRC_PASSWORD = os.environ.get("VRC_PASSWORD", "")
VRC_TOTP_SECRET = os.environ.get("VRC_TOTP_SECRET", "")
VRC_GROUP_ID = os.environ.get("VRC_GROUP_ID", "")
VRC_USER_AGENT = os.environ.get(
    "VRC_USER_AGENT", "VRCGangMediaBot/1.0 (set VRC_USER_AGENT to your own contact info)"
)
# Falls back to MODERATOR_ROLE_ID, same convention as activity.ADMIN_ROLE_ID.
ADMIN_ROLE_ID = int(os.environ.get("ADMIN_ROLE_ID") or os.environ.get("MODERATOR_ROLE_ID") or "0")
DB_PATH = Path(os.environ.get("VRC_LINKS_DB_FILE", "/app/data/vrc_links.json"))

# Role granted to linked members whose VRChat profile publicly shows the 18+
# verification badge. Leave unset to disable this check entirely.
VRC_ADULT_ROLE_ID = int(os.environ.get("VRC_ADULT_ROLE_ID") or "0") or None
# Delay between each linked member's VRChat lookup during a pass, so a large
# membership doesn't burst past VRChat's API rate limits.
ADULT_CHECK_DELAY_SECONDS = float(os.environ.get("VRC_ADULT_CHECK_DELAY_SECONDS", "10"))
# Hours between full passes over every linked member.
ADULT_CHECK_INTERVAL_HOURS = float(os.environ.get("VRC_ADULT_CHECK_INTERVAL_HOURS", "24"))

CONFIGURED = bool(VRC_USERNAME and VRC_PASSWORD and VRC_TOTP_SECRET and VRC_GROUP_ID)

VRC_ID_HELP_URL = (
    "https://help.vrchat.com/hc/en-us/articles/"
    "4408181867027-How-to-find-IDs-and-Identifiers-in-VRChat-for-Users-Worlds-Avatars"
)

API_BASE = "https://api.vrchat.cloud/api/1"
USER_ID_RE = re.compile(r"usr_[0-9a-fA-F-]{36}")
INVITE_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 20

_client: discord.Client | None = None
_session: aiohttp.ClientSession | None = None
_authed = False
_auth_lock = asyncio.Lock()
_db_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_links() -> dict:
    if DB_PATH.exists():
        with open(DB_PATH) as f:
            return json.load(f)
    return {"links": {}, "pending": {}}


def save_links(db: dict):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    tmp.replace(DB_PATH)


def is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return bool(ADMIN_ROLE_ID) and any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)


def parse_vrc_user_id(raw: str) -> str | None:
    match = USER_ID_RE.search(raw.strip())
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# VRChat API client
# ---------------------------------------------------------------------------

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"User-Agent": VRC_USER_AGENT})
    return _session


async def _login() -> bool:
    session = await _get_session()
    auth = aiohttp.BasicAuth(quote(VRC_USERNAME, safe=""), quote(VRC_PASSWORD, safe=""))
    try:
        async with session.get(f"{API_BASE}/auth/user", auth=auth) as resp:
            body = await resp.text()
            if resp.status != 200:
                print(f"[vrchat] Login failed: HTTP {resp.status} — {body[:200]}")
                return False
            data = json.loads(body)
    except aiohttp.ClientError as e:
        print(f"[vrchat] Login request error: {e!r}")
        return False

    if data.get("requiresTwoFactorAuth"):
        code = pyotp.TOTP(VRC_TOTP_SECRET.replace(" ", "")).now()
        try:
            async with session.post(
                f"{API_BASE}/auth/twofactorauth/totp/verify", json={"code": code}
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    print(f"[vrchat] 2FA verify failed: HTTP {resp.status} — {body[:200]}")
                    return False
                verify = json.loads(body)
                if not verify.get("verified"):
                    print(f"[vrchat] 2FA verify rejected: {body[:200]}")
                    return False
        except aiohttp.ClientError as e:
            print(f"[vrchat] 2FA verify request error: {e!r}")
            return False

    print("[vrchat] Authenticated with VRChat.")
    return True


async def _ensure_authed() -> bool:
    global _authed
    if _authed:
        return True
    async with _auth_lock:
        if _authed:
            return True
        _authed = await _login()
        return _authed


async def _request(method: str, path: str, **kwargs) -> tuple[int, dict | None, str]:
    """Return (status, json_or_None, raw_text). Re-logs-in once and retries on 401."""
    global _authed
    if not await _ensure_authed():
        return 0, None, "not authenticated"

    session = await _get_session()
    url = f"{API_BASE}{path}"
    async with session.request(method, url, **kwargs) as resp:
        status = resp.status
        text = await resp.text()

    if status == 401:
        _authed = False
        if await _ensure_authed():
            session = await _get_session()
            async with session.request(method, url, **kwargs) as resp:
                status = resp.status
                text = await resp.text()

    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None
    return status, data, text


def _error_message(status: int, data: dict | None, text: str) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
    return text[:200] if text else f"HTTP {status}"


async def get_vrc_user(user_id: str) -> dict | None:
    status, data, _ = await _request("GET", f"/users/{user_id}")
    return data if status == 200 else None


async def _refresh_link_display_name(discord_id: int, link: dict) -> str:
    """Lazily re-fetch a linked account's current VRChat display name, persisting
    it if it changed, and return the name to use right now."""
    user_info = await get_vrc_user(link["vrc_user_id"])
    if user_info is None:
        return link.get("vrc_display_name", link["vrc_user_id"])

    display_name = user_info.get("displayName", link["vrc_user_id"])
    if display_name != link.get("vrc_display_name"):
        async with _db_lock:
            db = load_links()
            current = db["links"].get(str(discord_id))
            if current and current.get("vrc_user_id") == link["vrc_user_id"]:
                current["vrc_display_name"] = display_name
                save_links(db)
    return display_name


async def is_group_member(user_id: str) -> bool:
    # membershipStatus also covers "invited"/"requested"/etc — an invite alone
    # makes this endpoint return 200, so status 200 by itself is NOT proof of
    # actual membership, only "member" is.
    status, data, _ = await _request("GET", f"/groups/{VRC_GROUP_ID}/members/{user_id}")
    if status != 200 or not isinstance(data, dict):
        return False
    return data.get("membershipStatus") == "member"


async def invite_to_group(user_id: str) -> tuple[bool, str]:
    status, data, text = await _request(
        "POST", f"/groups/{VRC_GROUP_ID}/invites", json={"userId": user_id}
    )
    if status in (200, 201):
        return True, ""
    return False, _error_message(status, data, text)


async def kick_from_group(user_id: str) -> tuple[bool, str]:
    status, data, text = await _request("DELETE", f"/groups/{VRC_GROUP_ID}/members/{user_id}")
    if status in (200, 204):
        return True, ""
    return False, _error_message(status, data, text)


# ---------------------------------------------------------------------------
# /vrcjoin — self-service, requires accepting the invite to prove ownership
# ---------------------------------------------------------------------------

async def _poll_invite_acceptance(
    interaction: discord.Interaction, discord_id: int, vrc_id: str, display_name: str
):
    try:
        elapsed = 0
        while elapsed < INVITE_TIMEOUT_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

            if await is_group_member(vrc_id):
                async with _db_lock:
                    db = load_links()
                    pending = db["pending"].get(str(discord_id))
                    if not pending or pending["vrc_user_id"] != vrc_id:
                        return  # superseded by a newer /vrcjoin or admin link
                    del db["pending"][str(discord_id)]
                    new_link = {
                        "vrc_user_id": vrc_id,
                        "vrc_display_name": display_name,
                        "linked_at": datetime.now(timezone.utc).isoformat(),
                        "linked_by": "self",
                    }
                    db["links"][str(discord_id)] = new_link
                    save_links(db)
                if VRC_ADULT_ROLE_ID is not None:
                    try:
                        await _sync_one_adult_role(discord_id, new_link)
                    except Exception as e:
                        print(f"[vrchat] Error syncing 18+ role after link for {discord_id}: {e!r}")
                try:
                    await interaction.followup.send(
                        f"✅ Linked to **{display_name}**! You'll be removed from the VRChat "
                        f"group automatically if you leave or are banned from this server.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return

        async with _db_lock:
            db = load_links()
            pending = db["pending"].get(str(discord_id))
            if pending and pending["vrc_user_id"] == vrc_id:
                del db["pending"][str(discord_id)]
                save_links(db)
        try:
            await interaction.followup.send(
                f"⌛ You didn't accept the invite to **{display_name}** within 10 minutes. "
                f"Run `/vrcjoin` again if you'd still like to join.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
    except Exception as e:
        print(f"[vrchat] Error polling invite acceptance for discord user {discord_id}: {e!r}")


@app_commands.command(
    name="vrcjoin", description="Get invited to the VRChat group and link your account"
)
@app_commands.describe(profile="Your VRChat profile link or usr_... user ID (leave blank for instructions)")
async def vrcjoin_command(interaction: discord.Interaction, profile: Optional[str] = None):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    if not CONFIGURED:
        await interaction.followup.send("VRChat linking isn't configured on this server.", ephemeral=True)
        return

    if profile is None:
        await interaction.followup.send(
            "**Linking your VRChat account:**\n"
            "1. Open your profile in VRChat (desktop app, quest app, or vrchat.com) and copy its "
            "profile link, or find your `usr_...` user ID.\n"
            f"   Not sure how? See VRChat's guide: <{VRC_ID_HELP_URL}>\n"
            "2. Run `/vrcjoin profile:<paste it here>` to send yourself a group invite.",
            ephemeral=True,
        )
        return

    vrc_id = parse_vrc_user_id(profile)
    if vrc_id is None:
        await interaction.followup.send(
            "Couldn't find a VRChat user ID in that. Paste your profile link "
            "(e.g. `https://vrchat.com/home/user/usr_xxxxxxxx-...`) or the raw `usr_...` ID.\n"
            f"Not sure how to find it? See VRChat's guide: <{VRC_ID_HELP_URL}>",
            ephemeral=True,
        )
        return

    discord_id = interaction.user.id
    db = load_links()
    existing = db["links"].get(str(discord_id))
    if existing:
        current_name = await _refresh_link_display_name(discord_id, existing)
        await interaction.followup.send(
            f"You're already linked to **{current_name}**. "
            f"Ask an admin if you need this changed.",
            ephemeral=True,
        )
        return

    user_info = await get_vrc_user(vrc_id)
    if user_info is None:
        await interaction.followup.send("Couldn't find a VRChat user with that ID. Double check the link.", ephemeral=True)
        return
    display_name = user_info.get("displayName", vrc_id)

    if await is_group_member(vrc_id):
        await interaction.followup.send(
            f"**{display_name}** is already in the group, so there's no invite to accept. "
            f"Ask an admin to run `/vrclinkadmin` to link it for auto-removal on leaving.",
            ephemeral=True,
        )
        return

    ok, err = await invite_to_group(vrc_id)
    if not ok:
        await interaction.followup.send(f"Couldn't send a group invite to **{display_name}**: {err}", ephemeral=True)
        return

    async with _db_lock:
        db = load_links()
        db["pending"][str(discord_id)] = {
            "vrc_user_id": vrc_id,
            "vrc_display_name": display_name,
            "invited_at": datetime.now(timezone.utc).isoformat(),
        }
        save_links(db)

    asyncio.create_task(_poll_invite_acceptance(interaction, discord_id, vrc_id, display_name))

    await interaction.followup.send(
        f"Invite sent to **{display_name}**! Accept it in VRChat within 10 minutes to finish linking "
        f"your account — once linked, you'll be removed from the group automatically if you leave "
        f"or are banned from this server.",
        ephemeral=True,
    )


@app_commands.command(
    name="vrcunlink", description="Unlink your Discord account from your VRChat account"
)
async def vrcunlink_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    if not CONFIGURED:
        await interaction.followup.send("VRChat linking isn't configured on this server.", ephemeral=True)
        return

    discord_id = interaction.user.id
    async with _db_lock:
        db = load_links()
        link = db["links"].pop(str(discord_id), None)
        had_pending = db["pending"].pop(str(discord_id), None) is not None
        if link or had_pending:
            save_links(db)

    if link is None and not had_pending:
        await interaction.followup.send("You're not currently linked.", ephemeral=True)
        return

    if VRC_ADULT_ROLE_ID is not None and isinstance(interaction.user, discord.Member):
        role = interaction.guild.get_role(VRC_ADULT_ROLE_ID) if interaction.guild else None
        if role and role in interaction.user.roles:
            try:
                await interaction.user.remove_roles(role, reason="Unlinked VRChat account")
            except discord.Forbidden:
                print(f"[vrchat] Missing permission to remove 18+ role from {interaction.user}")

    await interaction.followup.send(
        "You've been unlinked. Leaving this Discord server will no longer remove you from the "
        "VRChat group — you're still in it if you'd already joined. Run `/vrcjoin` again any time "
        "to re-link.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /vrclinkadmin — admin-only, skips the accept-to-prove-ownership step
# ---------------------------------------------------------------------------

@app_commands.command(
    name="vrclinkadmin", description="[Admin] Manually link a member to a VRChat account"
)
@app_commands.describe(member="The Discord member to link", profile="Their VRChat profile link or usr_... user ID")
async def vrclinkadmin_command(interaction: discord.Interaction, member: discord.Member, profile: str):
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    if not is_admin(interaction):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    if not CONFIGURED:
        await interaction.followup.send("VRChat linking isn't configured on this server.", ephemeral=True)
        return

    vrc_id = parse_vrc_user_id(profile)
    if vrc_id is None:
        await interaction.followup.send(
            "Couldn't find a VRChat user ID in that. Paste a profile link or the raw `usr_...` ID.\n"
            f"Not sure how to find it? See VRChat's guide: <{VRC_ID_HELP_URL}>",
            ephemeral=True,
        )
        return

    user_info = await get_vrc_user(vrc_id)
    if user_info is None:
        await interaction.followup.send("Couldn't find a VRChat user with that ID.", ephemeral=True)
        return
    display_name = user_info.get("displayName", vrc_id)

    invite_note = ""
    if not await is_group_member(vrc_id):
        ok, err = await invite_to_group(vrc_id)
        if not ok:
            invite_note = f" (couldn't send a group invite: {err})"

    async with _db_lock:
        db = load_links()
        for uid in list(db["links"]):
            if uid != str(member.id) and db["links"][uid].get("vrc_user_id") == vrc_id:
                del db["links"][uid]
        db["pending"].pop(str(member.id), None)
        new_link = {
            "vrc_user_id": vrc_id,
            "vrc_display_name": display_name,
            "linked_at": datetime.now(timezone.utc).isoformat(),
            "linked_by": f"admin:{interaction.user.id}",
        }
        db["links"][str(member.id)] = new_link
        save_links(db)

    if VRC_ADULT_ROLE_ID is not None:
        try:
            await _sync_one_adult_role(member.id, new_link)
        except Exception as e:
            print(f"[vrchat] Error syncing 18+ role after admin link for {member.id}: {e!r}")

    await interaction.followup.send(
        f"Linked {member.mention} to **{display_name}**.{invite_note} "
        f"They'll be removed from the VRChat group automatically if they leave or are banned.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# 18+ verification role
# ---------------------------------------------------------------------------

async def _sync_one_adult_role(discord_id: int, link: dict) -> bool:
    """Add/remove VRC_ADULT_ROLE_ID on `discord_id` to match whether their linked
    VRChat account currently shows the 18+ badge. Returns True if a role changed."""
    user_info = await get_vrc_user(link["vrc_user_id"])
    if user_info is None:
        return False  # profile not resolvable right now — leave the role as-is
    is_adult = user_info.get("ageVerificationStatus") == "18+"

    changed = False
    for guild in _client.guilds:
        role = guild.get_role(VRC_ADULT_ROLE_ID)
        member = guild.get_member(discord_id)
        if role is None or member is None:
            continue
        has_role = role in member.roles
        try:
            if is_adult and not has_role:
                await member.add_roles(role, reason="VRChat profile shows 18+ verification")
                print(f"[vrchat] Added 18+ Verified role to {member.display_name}")
                changed = True
            elif not is_adult and has_role:
                await member.remove_roles(role, reason="VRChat profile no longer shows 18+ verification")
                print(f"[vrchat] Removed 18+ Verified role from {member.display_name}")
                changed = True
        except discord.Forbidden:
            print(f"[vrchat] Missing permission to update 18+ role for {member}")
    return changed


async def sync_adult_roles() -> int:
    """One pass over every confirmed link, paced by ADULT_CHECK_DELAY_SECONDS so a
    large membership doesn't burst past VRChat's API rate limits. Returns the
    number of role changes made."""
    if VRC_ADULT_ROLE_ID is None:
        return 0
    links = list(load_links()["links"].items())
    if not links:
        return 0

    print(f"[vrchat] Checking 18+ verification for {len(links)} linked member(s)")
    changed = 0
    for discord_id_str, link in links:
        try:
            if await _sync_one_adult_role(int(discord_id_str), link):
                changed += 1
        except Exception as e:
            print(f"[vrchat] Error checking 18+ status for {discord_id_str}: {e!r}")
        await asyncio.sleep(ADULT_CHECK_DELAY_SECONDS)
    print(f"[vrchat] 18+ verification check complete — {changed} role change(s)")
    return changed


async def adult_role_loop():
    """Runs every ADULT_CHECK_INTERVAL_HOURS — the first pass waits a full interval
    too, rather than firing immediately on startup. Use /checkadult for an
    on-demand pass."""
    await _client.wait_until_ready()
    if VRC_ADULT_ROLE_ID is None:
        print("[vrchat] VRC_ADULT_ROLE_ID not set; 18+ verification role disabled.")
        return
    while not _client.is_closed():
        await asyncio.sleep(ADULT_CHECK_INTERVAL_HOURS * 3600)
        try:
            await sync_adult_roles()
        except Exception as e:
            print(f"[vrchat] Error in adult_role_loop: {e!r}")


@app_commands.command(
    name="checkadult", description="[Admin] Manually run the 18+ verification check now"
)
async def checkadult_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if not CONFIGURED:
        await interaction.response.send_message("VRChat linking isn't configured on this server.", ephemeral=True)
        return
    if VRC_ADULT_ROLE_ID is None:
        await interaction.response.send_message("VRC_ADULT_ROLE_ID is not configured.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    changed = await sync_adult_roles()
    try:
        await interaction.followup.send(
            f"18+ verification check complete — {changed} role change(s).", ephemeral=True
        )
    except discord.HTTPException:
        pass  # interaction token likely expired for a very large member list; check the logs


# ---------------------------------------------------------------------------
# Event handlers — called by bot.py
# ---------------------------------------------------------------------------

async def on_ready():
    if not CONFIGURED:
        print(
            "[vrchat] Not configured (VRC_USERNAME/VRC_PASSWORD/VRC_TOTP_SECRET/VRC_GROUP_ID) — "
            "VRChat linking disabled."
        )
        return
    if await _ensure_authed():
        print("[vrchat] Ready — logged into VRChat.")
    else:
        print("[vrchat] Failed to log into VRChat on startup; will retry on first command.")

    if VRC_ADULT_ROLE_ID is not None:
        asyncio.create_task(adult_role_loop())


async def handle_member_remove(member: discord.Member):
    """Fires for voluntary leaves, kicks, and bans alike — guild_member_remove
    covers all three, same as activity.handle_member_remove relies on."""
    if not CONFIGURED:
        return

    link = None
    async with _db_lock:
        db = load_links()
        link = db["links"].pop(str(member.id), None)
        had_pending = db["pending"].pop(str(member.id), None) is not None
        if link or had_pending:
            save_links(db)

    if link:
        ok, err = await kick_from_group(link["vrc_user_id"])
        if ok:
            print(f"[vrchat] Removed {member} ({member.id}) from VRChat group — left Discord.")
        else:
            print(f"[vrchat] Failed to remove {member} ({member.id}) from VRChat group: {err}")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

COMMANDS = [vrcjoin_command, vrcunlink_command, vrclinkadmin_command, checkadult_command]


def setup(client: discord.Client):
    """Attach the VRChat slash commands to `client`'s command tree."""
    global _client
    _client = client
    for command in COMMANDS:
        client.tree.add_command(command)
    print(f"[vrchat] Registered {len(COMMANDS)} command(s); db at {DB_PATH}; configured={CONFIGURED}")
