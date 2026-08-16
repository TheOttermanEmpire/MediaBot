# ToastBot

A Discord bot that enforces media-only channels, automatically creates threads for discussion, manages custom booster roles, cleans up voice channel text chats, and tracks member activity to find inactive members.

## Features

### Media enforcement

In configured channels, every message must contain either a file attachment or a URL. Messages that contain only text are deleted immediately and the author receives a temporary notification. This applies to both new messages and edits.

### Thread creation

When a user reacts to a message in a monitored channel with the 🧵 emoji, the bot:

1. Creates a thread on that message for discussion
2. Names the thread after the author — or, if the message contains an image, uses GPT-5.4 nano to generate a short descriptive title (5 words or less)
3. Removes the 🧵 reaction once the thread is created

The bot also adds a 🧵 reaction to every valid message automatically, so users know they can start a thread.

If nobody posts in a new thread within 5 minutes, the thread is deleted and the 🧵 reaction is put back on the original message, so an accidental reaction doesn't leave an empty thread behind. Discord's "X started a thread" announcement is also removed, since the thread is already visible on the source message.

If a message is deleted, any thread created from it is also automatically deleted.

### Custom booster roles

Server boosters can configure a personal role with a custom name and colour. Roles are ordered in the role list by boost date — the most OG boosters sit highest.

**`/role name:<name> color:<hex>`** — Set your own custom role name and colour. Colour accepts `#FF0000` or `FF0000` format. Only available to server boosters. Response is only visible to you.

**`/role name:<name> color:<hex> user:<member>`** — Moderators can use the optional `user:` parameter to set or update a role on behalf of another member.

**`/importrole user:<member> role:<role>`** — Moderators/admins can link an *existing* Discord role to a user, making it their managed booster role without creating a new one. Useful for migrating manually-created roles.

When a member stops boosting, their custom role is automatically removed from them but kept in the server. If they boost again and run `/role`, their existing role is reused and updated. The bot re-checks every hour, so lapsed boosters are caught even if the bot was offline when they stopped.

### Voice channel text cleanup

Messages in the built-in text chat of configured voice channels (Discord's "Open Chat" sidebar) are automatically deleted after 48 hours. The bot checks on startup to catch anything missed while offline, then runs once per hour. Use the voice channel's own ID — not a separate text channel.

The first time each user posts in one of these channels, the bot replies with a one-time warning that messages there are temporary. Each user is only ever warned once.

### Activity tracking

The bot records messages, reactions and voice channel time for every member, broken down by day and by channel. On startup it scans recent history so nothing is lost while it was offline.

| Command | Description |
|---|---|
| `/inactive` | Paginated list of all tracked members, sorted by how recently they were last seen, with 7-day activity counts |
| `/left` | Members who have left, showing whether they left voluntarily, were kicked, or were banned |
| `/stats user:<member> [days:<n>]` | Per-day and per-channel breakdown for one member (default 14 days, max 90) |
| `/vcstats [user:<member>] [days:<n>]` | Voice channel time — server-wide totals, or one member's breakdown |
| `/checkinactive [days:<n>]` | Run the inactivity check immediately instead of waiting for the daily pass |

These are admin-only: available to anyone with `ADMIN_ROLE_ID` (or `MODERATOR_ROLE_ID` if that isn't set), plus server administrators. All responses are only visible to you.

### Inactivity role

If `INACTIVE_ROLE_ID` is set, the bot checks once every 24 hours for members with no recorded activity in `INACTIVITY_DAYS` days and gives them the inactive role. Members holding any role in `IGNORED_ROLE_IDS` are skipped entirely.

Optionally, `REMOVE_ROLE_IDS_ON_INACTIVE` lists roles to strip when a member is tagged. Those roles are remembered and restored automatically when they come back.

**`/imback`** — Available to everyone. Removes the inactive role and restores any roles that were stripped.

Set `INACTIVE_CHANNEL_ID` to nominate a single channel that inactive members can see. The bot posts a status message there explaining `/imback` and deletes anything else posted in it. You need to set up the channel permission overwrites for `INACTIVE_ROLE_ID` yourself so it's the only channel they can see.

### VRChat group linking

Links a Discord member to a VRChat account, so that leaving, being kicked, or being banned from Discord also removes them from your VRChat group.

**`/vrcjoin`** — Available to everyone. Run with no arguments for instructions on finding your VRChat profile link/ID.

**`/vrcjoin profile:<link or usr_... ID>`** — Sends a VRChat group invite to the given account. The account isn't linked until the invite is **accepted in VRChat within 10 minutes** — accepting is what proves the Discord member actually owns that account. If it isn't accepted in time, the invite is dropped and they can try again. If the account is already a group member, there's nothing to accept, so it can only be linked by an admin.

**`/vrcunlink`** — Available to everyone. Removes your Discord↔VRChat link (and cancels a pending invite, if any). Leaving Discord afterward no longer removes you from the VRChat group — you stay in it if you'd already joined. It does not remove you from the group itself; leave the group in VRChat directly if that's what you want.

**`/vrclinkadmin member:<member> profile:<link or usr_... ID>`** — Admins can link any member to any VRChat account immediately, without the accept step (useful for accounts already in the group, or when the invite flow doesn't work). Sends a group invite as a courtesy if the account isn't already a member.

Requires a dedicated VRChat account for the bot to log into, with a group role granting invite and kick permissions in `VRC_GROUP_ID`. Set `VRC_USERNAME`, `VRC_PASSWORD`, `VRC_TOTP_SECRET` (its authenticator-app 2FA secret — email-code 2FA can't be automated), and `VRC_GROUP_ID` to enable this feature; leave them unset to disable it entirely.

If `VRC_ADULT_ROLE_ID` is set, the bot also keeps that role in sync with each linked member's VRChat 18+ verification badge (`ageVerificationStatus == "18+"` — the badge only shows when the account has verified *and* chosen to display it, not merely completed verification). It's applied immediately when a link is confirmed, then rechecked automatically every `VRC_ADULT_CHECK_INTERVAL_HOURS` (not on startup). Each pass checks one linked member every `VRC_ADULT_CHECK_DELAY_SECONDS` rather than all at once, to stay well under VRChat's API rate limits.

**`/checkadult`** — Admin-only (same role check as the activity commands). Runs an 18+ verification pass immediately instead of waiting for the next scheduled one.

## Configuration

All configuration is via environment variables.

### Core

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `MONITORED_GUILDS` | Yes | JSON map of guild IDs to lists of monitored channel IDs |
| `OPENAI_API_KEY` | For image titles | OpenAI API key (used for image titling via GPT-5.4 nano). Without it, threads fall back to being named after the author |

### Booster roles

| Variable | Required | Description |
|---|---|---|
| `BOOSTER_REQUIRED_ROLE_ID` | No | Role ID that counts as boosting (in addition to `premium_since`) |
| `MODERATOR_ROLE_ID` | No | Role ID that can use moderator features (`/role user:`, `/importrole`) |
| `BOOSTER_ROLE_ANCHOR_ID` | No | Role ID that sits directly above all custom booster roles — required for automatic role ordering |

### Voice channel cleanup

| Variable | Required | Description |
|---|---|---|
| `VOICE_TEXT_CHANNELS` | No | JSON array of voice channel IDs whose built-in text chat ("Open Chat") should be purged after 48 hours — e.g. `[123456789, 987654321]` (default `[]`) |

### Activity tracking and inactivity

| Variable | Required | Description |
|---|---|---|
| `ADMIN_ROLE_ID` | No | Role ID allowed to run the activity commands. Falls back to `MODERATOR_ROLE_ID`; server administrators always qualify |
| `EXCLUDED_USER_IDS` | No | Comma-separated user IDs to leave out of activity tracking entirely |
| `CATCHUP_HOURS` | No | How far back to scan on startup for activity missed while offline (default `48`) |
| `INACTIVE_ROLE_ID` | No | Role given to inactive members. Leave unset to disable the inactivity check entirely |
| `INACTIVITY_DAYS` | No | Days of no activity before a member is tagged (default `90`) |
| `IGNORED_ROLE_IDS` | No | Comma-separated role IDs exempt from the inactivity check |
| `INACTIVE_CHANNEL_ID` | No | The one channel inactive members can see |
| `REMOVE_ROLE_IDS_ON_INACTIVE` | No | Comma-separated role IDs to strip when tagging a member inactive, restored on `/imback` |

### VRChat group linking

| Variable | Required | Description |
|---|---|---|
| `VRC_USERNAME` | For this feature | Username of the dedicated VRChat account the bot logs in as |
| `VRC_PASSWORD` | For this feature | Password for that account |
| `VRC_TOTP_SECRET` | For this feature | Base32 TOTP secret for the account's authenticator-app 2FA |
| `VRC_GROUP_ID` | For this feature | The VRChat group ID (`grp_...`) to invite/remove members from |
| `VRC_USER_AGENT` | No | User-Agent sent to the VRChat API — replace the default with your own contact info per VRChat's API usage policy |
| `VRC_ADULT_ROLE_ID` | No | Role kept in sync with linked members' VRChat 18+ verification badge. Leave unset to disable this check |
| `VRC_ADULT_CHECK_DELAY_SECONDS` | No | Delay between each linked member's VRChat lookup during a pass (default `10`) |
| `VRC_ADULT_CHECK_INTERVAL_HOURS` | No | Hours between full passes over every linked member (default `24`) |

All four required variables must be set together; if any are missing, `/vrcjoin` and `/vrclinkadmin` respond that the feature isn't configured, and no members are ever removed from the group.

### Setting up `BOOSTER_ROLE_ANCHOR_ID`

Create a placeholder role in your server (e.g. `── Boosters ──`) and place it just above where you want custom booster roles to appear. Set `BOOSTER_ROLE_ANCHOR_ID` to its ID. The bot will stack all booster roles directly below it, sorted by boost date.

## Running with Docker

Copy `docker-compose.yaml`, fill in your credentials, then:

```bash
docker compose up -d
```

State is persisted to `./data` via a volume mount:

- `activity.json` — activity history, leave records, and stripped-role bookkeeping
- `booster_roles.json` — the member-to-role mapping for custom booster roles
- `voice_warned_users.json` — who has already seen the voice chat warning
- `vrc_links.json` — confirmed and pending Discord-to-VRChat account links

Back up `./data` before upgrading; deleting `activity.json` resets all activity history, deleting `booster_roles.json` orphans every custom booster role, and deleting `vrc_links.json` stops auto-removal from the VRChat group for everyone previously linked.

Each path can be overridden if you need a different layout:

| Variable | Default |
|---|---|
| `ACTIVITY_DB_FILE` | `/app/data/activity.json` |
| `BOOSTER_ROLES_FILE` | `/app/data/booster_roles.json` |
| `VOICE_WARNED_USERS_FILE` | `/app/data/voice_warned_users.json` |
| `VRC_LINKS_DB_FILE` | `/app/data/vrc_links.json` |

## Required bot permissions

- Read Messages / View Channels
- Send Messages
- Manage Messages (to delete non-media messages, old voice chat messages, and posts in the inactive channel)
- Add Reactions
- Manage Roles (to create, update, and assign booster and inactive roles)
- Manage Threads (to delete threads when their parent message is deleted or nobody replies)
- Create Public Threads
- View Audit Log (optional — used to tell kicks apart from voluntary leaves in `/left`; without it, kicks are recorded as ordinary leaves)

The bot's own role must sit above any role it manages, including custom booster roles, the inactive role, and `VRC_ADULT_ROLE_ID`.

## Required privileged intents

- **Message Content Intent** — to read message text for URL detection
- **Server Members Intent** — to detect when members stop boosting, fetch boost dates for role ordering, and track joins and leaves

Both must be enabled in the [Discord Developer Portal](https://discord.com/developers/applications) under your bot's settings.

Voice channel time tracking uses the Voice States intent, which is enabled by default and does not need to be turned on manually.
