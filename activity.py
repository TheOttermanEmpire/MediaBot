"""Member activity tracking, inactivity roles and stats commands.

This module registers no events of its own — bot.py owns the single
discord.Client and forwards the relevant events here via the handle_*
functions, since discord.py allows only one handler per event.
"""

import asyncio
import discord
import json
import os
from discord import app_commands
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Falls back to MODERATOR_ROLE_ID so the merged bot only needs one role configured.
ADMIN_ROLE_ID = int(os.environ.get("ADMIN_ROLE_ID") or os.environ.get("MODERATOR_ROLE_ID") or "0")
EXCLUDED_USER_IDS = set(
    int(x.strip()) for x in os.environ.get("EXCLUDED_USER_IDS", "").split(",") if x.strip()
)
CATCHUP_HOURS = int(os.environ.get("CATCHUP_HOURS", "48"))
INACTIVE_ROLE_ID = int(os.environ["INACTIVE_ROLE_ID"]) if os.environ.get("INACTIVE_ROLE_ID") else None
INACTIVITY_DAYS = int(os.environ.get("INACTIVITY_DAYS", "90"))
IGNORED_ROLE_IDS = set(
    int(x.strip()) for x in os.environ.get("IGNORED_ROLE_IDS", "").split(",") if x.strip()
)
INACTIVE_CHANNEL_ID = int(os.environ["INACTIVE_CHANNEL_ID"]) if os.environ.get("INACTIVE_CHANNEL_ID") else None
REMOVE_ROLE_IDS_ON_INACTIVE = set(
    int(x.strip()) for x in os.environ.get("REMOVE_ROLE_IDS_ON_INACTIVE", "").split(",") if x.strip()
)
DB_PATH = Path(os.environ.get("ACTIVITY_DB_FILE", "/app/data/activity.json"))
PAGE_SIZE = 15
META_KEY = "_meta"  # non-numeric top-level db key reserved for bot state, never a user id
PAGE_VIEW_TIMEOUT = 900

STATUS_MESSAGE_TEXT = (
    "You've been marked **inactive** and moved here — this is the only channel you can see.\n\n"
    "Type **/imback** to remove the inactive role and regain full access."
)

# user_id -> (channel_id, join_time)
_vc_join_times: dict[int, tuple[int, datetime]] = {}
_status_message_id: int | None = None
_pending_bans: set[int] = set()

_client: discord.Client | None = None
_started = False


# ---------------------------------------------------------------------------
# DB helpers
#
# Every read-modify-write below must stay free of `await` between load_db() and
# save_db(), otherwise concurrent event handlers clobber each other's writes.
# ---------------------------------------------------------------------------

def load_db() -> dict:
    if DB_PATH.exists():
        with open(DB_PATH) as f:
            return json.load(f)
    return {}


def save_db(db: dict):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    tmp.replace(DB_PATH)


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    h, m = m // 60, m % 60
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def _ensure_user(db: dict, user_id: int) -> dict:
    uid = str(user_id)
    if uid not in db:
        db[uid] = {}
    record = db[uid]
    for key in ("messages", "reactions", "voice"):
        record.setdefault(key, {})
    return record


def record_message(db: dict, user_id: int, channel_id: int, timestamp: datetime) -> bool:
    if user_id in EXCLUDED_USER_IDS:
        return False
    record = _ensure_user(db, user_id)
    cid = str(channel_id)
    day = timestamp.date().isoformat()
    day_data = record["messages"].setdefault(day, {})
    day_data[cid] = day_data.get(cid, 0) + 1
    ts = timestamp.isoformat()
    if record.get("last_message", "") < ts:
        record["last_message"] = ts
    record.pop("left_at", None)
    record.pop("leave_reason", None)
    return True


def record_reaction(db: dict, user_id: int, channel_id: int, timestamp: datetime) -> bool:
    if user_id in EXCLUDED_USER_IDS:
        return False
    record = _ensure_user(db, user_id)
    cid = str(channel_id)
    day = timestamp.date().isoformat()
    day_data = record["reactions"].setdefault(day, {})
    day_data[cid] = day_data.get(cid, 0) + 1
    ts = timestamp.isoformat()
    if record.get("last_reaction", "") < ts:
        record["last_reaction"] = ts
    record.pop("left_at", None)
    record.pop("leave_reason", None)
    return True


def add_vc_time(db: dict, user_id: int, channel_id: int, day: str, seconds: int) -> bool:
    if user_id in EXCLUDED_USER_IDS or seconds <= 0:
        return False
    record = _ensure_user(db, user_id)
    cid = str(channel_id)
    day_data = record["voice"].setdefault(day, {})
    day_data[cid] = day_data.get(cid, 0) + seconds
    return True


def _overall_last_seen(data: dict) -> str:
    return max(
        data.get("last_message", ""),
        data.get("last_reaction", ""),
        data.get("last_voice", ""),
        data.get("last_seen", ""),  # compat with old records
    )


def record_leave(db: dict, member: discord.Member, reason: str) -> bool:
    if member.id in EXCLUDED_USER_IDS:
        return False
    uid = str(member.id)
    left_at = datetime.now(timezone.utc).isoformat()
    record = db.get(uid, {})
    record["left_at"] = left_at
    record["leave_reason"] = reason
    record["display_name"] = member.display_name
    record.setdefault("last_seen", _overall_last_seen(record) or left_at)
    db[uid] = record
    return True


def sum_stat(data: dict, stat_key: str, since_day: str | None = None) -> int:
    total = 0
    for day, channels in data.get(stat_key, {}).items():
        if since_day and day < since_day:
            continue
        total += sum(channels.values())
    return total


def sum_stat_by_channel(data: dict, stat_key: str, since_day: str | None = None) -> dict[str, int]:
    totals: dict[str, int] = {}
    for day, channels in data.get(stat_key, {}).items():
        if since_day and day < since_day:
            continue
        for cid, val in channels.items():
            totals[cid] = totals.get(cid, 0) + val
    return totals


def days_with_stat(data: dict, stat_key: str, since_day: str | None = None) -> list[tuple[str, int]]:
    """Return (day, total) pairs sorted descending, optionally filtered to since_day."""
    result = []
    for day, channels in data.get(stat_key, {}).items():
        if since_day and day < since_day:
            continue
        total = sum(channels.values())
        if total:
            result.append((day, total))
    result.sort(reverse=True)
    return result


# ---------------------------------------------------------------------------
# Shared pagination view
# ---------------------------------------------------------------------------

class PageView(discord.ui.View):
    def __init__(self, entries: list, build_embed_fn):
        super().__init__(timeout=PAGE_VIEW_TIMEOUT)
        self.entries = entries
        self.build_embed_fn = build_embed_fn
        self.page = 0
        self.total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    async def on_timeout(self):
        self.prev_btn.disabled = True
        self.next_btn.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.entries, self.page), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed_fn(self.entries, self.page), view=self)


def is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return bool(ADMIN_ROLE_ID) and any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)


# ---------------------------------------------------------------------------
# /inactive
# ---------------------------------------------------------------------------

def build_inactive_embed(entries: list, page: int, guild: discord.Guild | None) -> discord.Embed:
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_entries = entries[start: start + PAGE_SIZE]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()

    embed = discord.Embed(title="Member Activity", color=discord.Color.blurple())
    if not entries:
        embed.description = "No activity recorded yet."
        embed.set_footer(text="Page 1/1 • 0 members tracked")
        return embed

    lines = []
    for i, (uid, data) in enumerate(page_entries, start=start + 1):
        member = guild.get_member(int(uid)) if guild else None
        name = member.display_name if member else data.get("display_name", f"Unknown ({uid})")

        last_parts = []
        for field, emoji in [("last_message", "💬"), ("last_reaction", "👍"), ("last_voice", "🔊")]:
            ts_str = data.get(field)
            if ts_str:
                unix = int(datetime.fromisoformat(ts_str).timestamp())
                last_parts.append(f"{emoji}<t:{unix}:R>")
        # compat: old single last_seen field
        if not last_parts and data.get("last_seen"):
            unix = int(datetime.fromisoformat(data["last_seen"]).timestamp())
            last_parts.append(f"❓<t:{unix}:R>")

        count_parts = []
        msgs_7d = sum_stat(data, "messages", week_ago)
        reacts_7d = sum_stat(data, "reactions", week_ago)
        voice_7d = sum_stat(data, "voice", week_ago)
        if msgs_7d:
            count_parts.append(f"💬{msgs_7d}")
        if reacts_7d:
            count_parts.append(f"👍{reacts_7d}")
        if voice_7d:
            count_parts.append(f"🔊{format_duration(voice_7d)}")

        last_str = " • ".join(last_parts) if last_parts else "no activity"
        count_str = f"  _(7d: {' '.join(count_parts)})_" if count_parts else ""
        lines.append(f"`{i:>3}.` **{name}** — {last_str}{count_str}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(entries)} members tracked")
    return embed


@app_commands.command(name="inactive", description="Show member activity stats (admin only)")
async def inactive_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    entries = sorted(
        [(uid, data) for uid, data in db.items() if uid != META_KEY and "left_at" not in data],
        key=lambda x: _overall_last_seen(x[1]),
        reverse=True,
    )
    guild = interaction.guild
    view = PageView(entries, lambda e, p: build_inactive_embed(e, p, guild))
    await interaction.response.send_message(
        embed=build_inactive_embed(entries, 0, guild), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# /left
# ---------------------------------------------------------------------------

def build_left_embed(entries: list, page: int) -> discord.Embed:
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_entries = entries[start: start + PAGE_SIZE]

    embed = discord.Embed(title="Members Who Left", color=discord.Color.red())
    if not entries:
        embed.description = "No members have left (that were tracked)."
        embed.set_footer(text="Page 1/1 • 0 members")
        return embed

    LEAVE_EMOJI = {"left": "🚪", "kicked": "👢", "banned": "🔨"}
    lines = []
    for i, (uid, data) in enumerate(page_entries, start=start + 1):
        left_unix = int(datetime.fromisoformat(data["left_at"]).timestamp())
        last_str = data.get("last_seen") or _overall_last_seen(data)
        last_unix = int(datetime.fromisoformat(last_str).timestamp()) if last_str else left_unix
        name = data.get("display_name", f"Unknown ({uid})")
        emoji = LEAVE_EMOJI.get(data.get("leave_reason", "left"), "🚪")
        lines.append(f"`{i:>3}.` {emoji} **{name}** — left <t:{left_unix}:R> • last active <t:{last_unix}:R>")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • {len(entries)} members")
    return embed


@app_commands.command(name="left", description="Show members who have left the server (admin only)")
async def left_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    entries = sorted(
        [(uid, data) for uid, data in db.items() if uid != META_KEY and "left_at" in data],
        key=lambda x: x[1]["left_at"],
        reverse=True,
    )
    view = PageView(entries, build_left_embed)
    await interaction.response.send_message(
        embed=build_left_embed(entries, 0), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

def _channel_name(guild: discord.Guild | None, cid: str) -> str:
    if guild:
        ch = guild.get_channel(int(cid))
        if ch:
            return f"#{ch.name}"
    return f"<#{cid}>"


def _top_channels_field(data: dict, stat_key: str, since_day: str | None, guild, fmt=str) -> str:
    totals = sum_stat_by_channel(data, stat_key, since_day)
    if not totals:
        return "—"
    top = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:5]
    return "\n".join(f"{_channel_name(guild, cid)}: {fmt(val)}" for cid, val in top)


@app_commands.command(name="stats", description="Show granular per-day stats for a user (admin only)")
@app_commands.describe(
    user="The user to inspect",
    days="How many days to look back (default 14, max 90)",
)
async def stats_command(interaction: discord.Interaction, user: discord.Member, days: int = 14):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    days = max(1, min(days, 90))
    db = load_db()
    data = db.get(str(user.id), {})
    guild = interaction.guild
    since_day = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    total_msgs = sum_stat(data, "messages", since_day)
    total_reacts = sum_stat(data, "reactions", since_day)
    total_voice_s = sum_stat(data, "voice", since_day)

    embed = discord.Embed(
        title=f"Stats — {user.display_name}",
        description=(
            f"Last **{days}** days: "
            f"💬 **{total_msgs}** msgs  •  "
            f"👍 **{total_reacts}** reactions  •  "
            f"🔊 **{format_duration(total_voice_s)}** voice"
        ),
        color=discord.Color.blurple(),
    )

    # Messages per day + top channels
    if total_msgs:
        day_lines = [f"`{day}` {count}" for day, count in days_with_stat(data, "messages", since_day)[:20]]
        embed.add_field(name="💬 Messages per day", value="\n".join(day_lines) or "—", inline=True)
        embed.add_field(
            name="💬 Top channels",
            value=_top_channels_field(data, "messages", since_day, guild),
            inline=True,
        )
        embed.add_field(name="​", value="​", inline=True)  # spacer

    # Reactions per day + top channels
    if total_reacts:
        day_lines = [f"`{day}` {count}" for day, count in days_with_stat(data, "reactions", since_day)[:20]]
        embed.add_field(name="👍 Reactions per day", value="\n".join(day_lines) or "—", inline=True)
        embed.add_field(
            name="👍 Top channels",
            value=_top_channels_field(data, "reactions", since_day, guild),
            inline=True,
        )
        embed.add_field(name="​", value="​", inline=True)

    # Voice per day + top channels
    if total_voice_s:
        day_lines = [
            f"`{day}` {format_duration(secs)}"
            for day, secs in days_with_stat(data, "voice", since_day)[:20]
        ]
        embed.add_field(name="🔊 Voice per day", value="\n".join(day_lines) or "—", inline=True)
        embed.add_field(
            name="🔊 Top channels",
            value=_top_channels_field(data, "voice", since_day, guild, format_duration),
            inline=True,
        )
        embed.add_field(name="​", value="​", inline=True)

    if not total_msgs and not total_reacts and not total_voice_s:
        embed.description += "\n\nNo activity in this period."

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /vcstats
# ---------------------------------------------------------------------------

@app_commands.command(name="vcstats", description="Show voice channel time stats (admin only)")
@app_commands.describe(
    user="Per-channel breakdown for a specific user (optional)",
    days="Only count the last N days (optional)",
)
async def vcstats_command(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    days: Optional[int] = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    guild = interaction.guild
    since_day = (
        (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat() if days else None
    )
    period_label = f" (last {days}d)" if days else ""

    if user is None:
        # Aggregate across all users
        channel_totals: dict[str, int] = {}
        channel_users: dict[str, set] = {}
        for uid, data in db.items():
            if uid == META_KEY:
                continue
            for cid, secs in sum_stat_by_channel(data, "voice", since_day).items():
                channel_totals[cid] = channel_totals.get(cid, 0) + secs
                channel_users.setdefault(cid, set()).add(uid)

        embed = discord.Embed(title=f"Voice Channel Stats{period_label}", color=discord.Color.green())
        if not channel_totals:
            embed.description = "No voice channel time tracked yet."
        else:
            lines = []
            for cid, secs in sorted(channel_totals.items(), key=lambda x: x[1], reverse=True):
                n = len(channel_users.get(cid, set()))
                lines.append(
                    f"**{_channel_name(guild, cid)}** — {format_duration(secs)} "
                    f"({n} user{'s' if n != 1 else ''})"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Total: {format_duration(sum(channel_totals.values()))}")
    else:
        data = db.get(str(user.id), {})
        ch_totals = sum_stat_by_channel(data, "voice", since_day)
        embed = discord.Embed(
            title=f"Voice Stats — {user.display_name}{period_label}",
            color=discord.Color.green(),
        )
        if not ch_totals:
            embed.description = "No voice time tracked."
        else:
            lines = [
                f"**{_channel_name(guild, cid)}** — {format_duration(secs)}"
                for cid, secs in sorted(ch_totals.items(), key=lambda x: x[1], reverse=True)
            ]
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Total: {format_duration(sum(ch_totals.values()))}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Startup / background loops
# ---------------------------------------------------------------------------

async def catch_up():
    """Scan recent history so activity missed while the bot was down is recorded.

    Findings are buffered and applied in one load→mutate→save so the long scan
    can't clobber writes made by live events in the meantime.
    """
    print(f"[activity] Starting catch-up scan for the last {CATCHUP_HOURS} hours...")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CATCHUP_HOURS)
    messages: list[tuple[int, int, datetime]] = []
    reactions: list[tuple[int, int, datetime]] = []
    in_voice: list[tuple[int, int]] = []

    for guild in _client.guilds:
        for channel in guild.text_channels:
            try:
                async for message in channel.history(after=cutoff, limit=None):
                    if message.author.bot or message.author.id in EXCLUDED_USER_IDS:
                        continue
                    messages.append((message.author.id, channel.id, message.created_at))
                    for reaction in message.reactions:
                        async for u in reaction.users():
                            if u.bot or u.id in EXCLUDED_USER_IDS:
                                continue
                            reactions.append((u.id, channel.id, message.created_at))
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f"[activity] Error scanning #{channel.name}: {e}")

        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot or member.id in EXCLUDED_USER_IDS:
                    continue
                in_voice.append((member.id, vc.id))

    now = datetime.now(timezone.utc)
    now_ts = now.isoformat()
    db = load_db()
    changed = False

    for user_id, channel_id, ts in messages:
        changed = record_message(db, user_id, channel_id, ts) or changed
    for user_id, channel_id, ts in reactions:
        changed = record_reaction(db, user_id, channel_id, ts) or changed
    for user_id, channel_id in in_voice:
        _vc_join_times[user_id] = (channel_id, now)
        record = _ensure_user(db, user_id)
        if record.get("last_voice", "") < now_ts:
            record["last_voice"] = now_ts
            changed = True

    if changed:
        save_db(db)
    print("[activity] Catch-up complete.")


async def reconcile_vc_loop():
    """Every 5 minutes, finalize sessions for users no longer in their tracked channel."""
    await _client.wait_until_ready()
    while not _client.is_closed():
        await asyncio.sleep(300)

        currently_in_vc: dict[int, int] = {}
        for guild in _client.guilds:
            for vc in guild.voice_channels:
                for member in vc.members:
                    if not member.bot and member.id not in EXCLUDED_USER_IDS:
                        currently_in_vc[member.id] = vc.id

        now = datetime.now(timezone.utc)
        to_finalize: list[tuple[int, int, datetime]] = []

        for user_id, (channel_id, join_time) in list(_vc_join_times.items()):
            actual = currently_in_vc.get(user_id)
            if actual is None:
                to_finalize.append((user_id, channel_id, join_time))
                del _vc_join_times[user_id]
            elif actual != channel_id:
                to_finalize.append((user_id, channel_id, join_time))
                _vc_join_times[user_id] = (actual, now)

        if to_finalize:
            db = load_db()
            for user_id, channel_id, join_time in to_finalize:
                elapsed = int((now - join_time).total_seconds())
                day = join_time.date().isoformat()
                add_vc_time(db, user_id, channel_id, day, elapsed)
            save_db(db)


async def mark_inactive_members(threshold_days: int | None = None) -> list[discord.Member]:
    """Add INACTIVE_ROLE_ID to members with no recorded activity in `threshold_days` days
    (defaults to INACTIVITY_DAYS). Returns the list of members that were newly tagged."""
    if INACTIVE_ROLE_ID is None:
        return []

    threshold_days = threshold_days if threshold_days is not None else INACTIVITY_DAYS
    db = load_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=threshold_days)
    tagged: list[discord.Member] = []
    stripped: dict[int, list[int]] = {}

    for guild in _client.guilds:
        role = guild.get_role(INACTIVE_ROLE_ID)
        if role is None:
            print(f"[activity] INACTIVE_ROLE_ID {INACTIVE_ROLE_ID} not found in guild {guild.name}; skipping.")
            continue

        for member in guild.members:
            if member.bot or member.id in EXCLUDED_USER_IDS:
                continue
            if any(r.id in IGNORED_ROLE_IDS for r in member.roles):
                continue
            if role in member.roles:
                continue

            data = db.get(str(member.id), {})
            last_str = _overall_last_seen(data)
            last_active = datetime.fromisoformat(last_str) if last_str else (member.joined_at or now)
            if last_active <= cutoff:
                try:
                    await member.add_roles(role, reason=f"No activity for {threshold_days}+ days")
                    tagged.append(member)
                except discord.Forbidden:
                    print(f"[activity] Missing permission to add inactive role to {member}.")
                    continue
                except discord.HTTPException as e:
                    print(f"[activity] Failed to add inactive role to {member}: {e}")
                    continue

                to_strip = [r for r in member.roles if r.id in REMOVE_ROLE_IDS_ON_INACTIVE]
                if to_strip:
                    try:
                        await member.remove_roles(*to_strip, reason="Marked inactive")
                        stripped[member.id] = [r.id for r in to_strip]
                    except discord.Forbidden:
                        print(f"[activity] Missing permission to remove roles from {member}.")
                    except discord.HTTPException as e:
                        print(f"[activity] Failed to remove roles from {member}: {e}")

    if stripped:
        db = load_db()
        for user_id, role_ids in stripped.items():
            _ensure_user(db, user_id)["stripped_role_ids"] = role_ids
        save_db(db)
    return tagged


async def inactive_role_loop():
    """Once every 24h, tag members who've had no activity in INACTIVITY_DAYS days."""
    await _client.wait_until_ready()
    if INACTIVE_ROLE_ID is None:
        print("[activity] INACTIVE_ROLE_ID not set; inactive-role loop disabled.")
        return
    while not _client.is_closed():
        try:
            tagged = await mark_inactive_members()
            if tagged:
                print(f"[activity] Inactive role loop: tagged {len(tagged)} member(s).")
        except Exception as e:
            print(f"[activity] Error in inactive_role_loop: {e}")
        await asyncio.sleep(86400)


# ---------------------------------------------------------------------------
# /checkinactive, /imback and the inactive-only channel
# ---------------------------------------------------------------------------

@app_commands.command(name="checkinactive", description="Manually run the inactivity check now (admin only)")
@app_commands.describe(days="Override the inactivity threshold in days for this run only (optional)")
async def checkinactive_command(interaction: discord.Interaction, days: Optional[int] = None):
    if not is_admin(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    if INACTIVE_ROLE_ID is None:
        await interaction.response.send_message("INACTIVE_ROLE_ID is not configured.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    tagged = await mark_inactive_members(days)
    threshold = days if days is not None else INACTIVITY_DAYS

    if not tagged:
        await interaction.followup.send(f"No members inactive for {threshold}+ days were found.", ephemeral=True)
        return

    names = "\n".join(f"- {m.display_name}" for m in tagged[:25])
    extra = f"\n… and {len(tagged) - 25} more" if len(tagged) > 25 else ""
    await interaction.followup.send(
        f"Tagged **{len(tagged)}** member(s) inactive for {threshold}+ days:\n{names}{extra}",
        ephemeral=True,
    )


def build_status_embed() -> discord.Embed:
    return discord.Embed(
        title="You've been marked inactive",
        description=STATUS_MESSAGE_TEXT,
        color=discord.Color.orange(),
    )


async def ensure_status_message():
    """Make sure the pinned-style status message exists in INACTIVE_CHANNEL_ID, recreating it if missing."""
    global _status_message_id
    if INACTIVE_CHANNEL_ID is None:
        return

    channel = _client.get_channel(INACTIVE_CHANNEL_ID)
    if channel is None:
        try:
            channel = await _client.fetch_channel(INACTIVE_CHANNEL_ID)
        except discord.HTTPException as e:
            print(f"[activity] Could not access INACTIVE_CHANNEL_ID {INACTIVE_CHANNEL_ID}: {e}")
            return

    msg_id = load_db().get(META_KEY, {}).get("inactive_status_message_id")

    message = None
    if msg_id:
        try:
            message = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        message = await channel.send(embed=build_status_embed())
        db = load_db()
        db.setdefault(META_KEY, {})["inactive_status_message_id"] = message.id
        save_db(db)

    _status_message_id = message.id


@app_commands.command(name="imback", description="Remove the inactive role and regain full access")
async def imback_command(interaction: discord.Interaction):
    if INACTIVE_ROLE_ID is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("The inactive role isn't configured.", ephemeral=True)
        return

    role = interaction.guild.get_role(INACTIVE_ROLE_ID) if interaction.guild else None
    if role is None or role not in interaction.user.roles:
        await interaction.response.send_message("You don't currently have the inactive role.", ephemeral=True)
        return

    try:
        await interaction.user.remove_roles(role, reason="Used /imback")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to remove that role.", ephemeral=True)
        return

    db = load_db()
    record = db.get(str(interaction.user.id), {})
    stripped_ids = record.pop("stripped_role_ids", None)
    restored_names = []
    if stripped_ids:
        save_db(db)
        to_restore = [interaction.guild.get_role(rid) for rid in stripped_ids]
        to_restore = [r for r in to_restore if r is not None]
        if to_restore:
            try:
                await interaction.user.add_roles(*to_restore, reason="Restored on /imback")
                restored_names = [r.name for r in to_restore]
            except discord.Forbidden:
                print(f"[activity] Missing permission to restore roles for {interaction.user}.")
            except discord.HTTPException as e:
                print(f"[activity] Failed to restore roles for {interaction.user}: {e}")

    suffix = f" Restored: {', '.join(restored_names)}." if restored_names else ""
    await interaction.response.send_message(f"Welcome back! You now have full access again.{suffix}", ephemeral=True)


# ---------------------------------------------------------------------------
# Event handlers — called by bot.py
# ---------------------------------------------------------------------------

async def on_ready():
    """Run the startup scan and kick off the background loops (once per process)."""
    global _started
    if _started:
        return
    _started = True
    await catch_up()
    await ensure_status_message()
    asyncio.create_task(reconcile_vc_loop())
    asyncio.create_task(inactive_role_loop())


async def handle_message(message: discord.Message) -> bool:
    """Record the message as activity.

    Returns True if the message belongs to the inactive-only channel and the
    caller should stop processing it.
    """
    if INACTIVE_CHANNEL_ID is not None and message.channel.id == INACTIVE_CHANNEL_ID:
        if not message.author.bot and message.id != _status_message_id:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        return True

    if message.author.bot:
        return False

    db = load_db()
    if record_message(db, message.author.id, message.channel.id, message.created_at):
        save_db(db)
    return False


async def handle_raw_reaction_add(payload: discord.RawReactionActionEvent):
    member = payload.member
    if member is None and payload.guild_id:
        guild = _client.get_guild(payload.guild_id)
        if guild:
            member = guild.get_member(payload.user_id)
    if member and member.bot:
        return
    db = load_db()
    if record_reaction(db, payload.user_id, payload.channel_id, datetime.now(timezone.utc)):
        save_db(db)


async def handle_member_ban(guild: discord.Guild, user: discord.User):
    _pending_bans.add(user.id)


async def handle_member_remove(member: discord.Member):
    if member.bot or member.id in EXCLUDED_USER_IDS:
        return
    if member.id in _pending_bans:
        _pending_bans.discard(member.id)
        reason = "banned"
    else:
        await asyncio.sleep(1)
        reason = "left"
        try:
            async for entry in member.guild.audit_logs(action=discord.AuditLogAction.kick, limit=10):
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if entry.target.id == member.id and age < 10:
                    reason = "kicked"
                    break
        except discord.Forbidden:
            pass

    _vc_join_times.pop(member.id, None)
    db = load_db()
    if record_leave(db, member, reason):
        save_db(db)


async def handle_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    now = datetime.now(timezone.utc)
    db = load_db()
    changed = False

    # Leaving or switching away — finalise the session.
    if before.channel is not None and member.id in _vc_join_times:
        joined_channel_id, join_time = _vc_join_times.pop(member.id)
        elapsed = int((now - join_time).total_seconds())
        day = join_time.date().isoformat()
        if add_vc_time(db, member.id, joined_channel_id, day, elapsed):
            changed = True

    # Joining or switching to a new channel — start a fresh session.
    if after.channel is not None:
        if member.id not in EXCLUDED_USER_IDS:
            _vc_join_times[member.id] = (after.channel.id, now)
        record = _ensure_user(db, member.id)
        ts = now.isoformat()
        if record.get("last_voice", "") < ts:
            record["last_voice"] = ts
            changed = True

    if changed:
        save_db(db)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

COMMANDS = [
    inactive_command,
    left_command,
    stats_command,
    vcstats_command,
    checkinactive_command,
    imback_command,
]


def setup(client: discord.Client):
    """Attach the activity slash commands to `client`'s command tree."""
    global _client
    _client = client
    for command in COMMANDS:
        client.tree.add_command(command)
    print(f"[activity] Registered {len(COMMANDS)} command(s); db at {DB_PATH}")
