# MediaBot

A Discord bot that enforces media-only channels, automatically creates threads for discussion, manages custom booster roles, and cleans up voice channel text chats.

## Features

### Media enforcement

In configured channels, every message must contain either a file attachment or a URL. Messages that contain only text are deleted immediately and the author receives a temporary notification. This applies to both new messages and edits.

### Thread creation

When a user reacts to a message in a monitored channel with the 🧵 emoji, the bot:

1. Creates a thread on that message for discussion
2. Names the thread after the author — or, if the message contains an image, uses GPT-4o mini to generate a short descriptive title (5 words or less)
3. Removes the 🧵 reaction once the thread is created

The bot also adds a 🧵 reaction to every valid message automatically, so users know they can start a thread.

If a message is deleted, any thread created from it is also automatically deleted.

### Custom booster roles

Server boosters can configure a personal role with a custom name and colour. Roles are ordered in the role list by boost date — the most OG boosters sit highest.

**`/role name:<name> color:<hex>`** — Set your own custom role name and colour. Colour accepts `#FF0000` or `FF0000` format. Only available to server boosters. Response is only visible to you.

**`/role name:<name> color:<hex> user:<member>`** — Moderators can use the optional `user:` parameter to set or update a role on behalf of another member.

**`/importrole user:<member> role:<role>`** — Moderators/admins can link an *existing* Discord role to a user, making it their managed booster role without creating a new one. Useful for migrating manually-created roles.

When a member stops boosting, their custom role is automatically removed from them but kept in the server. If they boost again and run `/role`, their existing role is reused and updated.

### Voice channel text cleanup

Messages in the built-in text chat of configured voice channels (Discord's "Open Chat" sidebar) are automatically deleted after 48 hours. The bot checks on startup to catch anything missed while offline, then runs once per hour. Use the voice channel's own ID — not a separate text channel.

## Configuration

All configuration is via environment variables:

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord bot token |
| `OPENAI_API_KEY` | Yes | OpenAI API key (used for image titling via GPT-4o mini) |
| `MONITORED_GUILDS` | Yes | JSON map of guild IDs to lists of monitored channel IDs |
| `BOOSTER_REQUIRED_ROLE_ID` | No | Role ID that counts as boosting (in addition to `premium_since`) |
| `MODERATOR_ROLE_ID` | No | Role ID that can use moderator features (`/role user:`, `/importrole`) |
| `BOOSTER_ROLE_ANCHOR_ID` | No | Role ID that sits directly above all custom booster roles — required for automatic role ordering |
| `VOICE_TEXT_CHANNELS` | No | JSON array of voice channel IDs whose built-in text chat ("Open Chat") should be purged after 48 hours — e.g. `[123456789, 987654321]` (default `[]`) |

### Setting up `BOOSTER_ROLE_ANCHOR_ID`

Create a placeholder role in your server (e.g. `── Boosters ──`) and place it just above where you want custom booster roles to appear. Set `BOOSTER_ROLE_ANCHOR_ID` to its ID. The bot will stack all booster roles directly below it, sorted by boost date.

## Running with Docker

Copy `docker-compose.yaml`, fill in your credentials, then:

```bash
docker compose up -d
```

Booster role data is persisted in `./data/booster_roles.json` via a volume mount.

## Required bot permissions

- Read Messages / View Channels
- Send Messages
- Manage Messages (to delete non-media messages and old voice chat messages)
- Add Reactions
- Manage Roles (to create, update, and assign booster roles)
- Manage Threads (to delete threads when their parent message is deleted)
- Create Public Threads

## Required privileged intents

- **Message Content Intent** — to read message text for URL detection
- **Server Members Intent** — to detect when members stop boosting and to fetch member boost dates for role ordering

Both must be enabled in the [Discord Developer Portal](https://discord.com/developers/applications) under your bot's settings.
