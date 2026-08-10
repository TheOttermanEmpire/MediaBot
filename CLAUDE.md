# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process Discord bot (discord.py) for one server, deployed via Docker Compose. Four independent feature areas share one `discord.Client`: media-only channel enforcement + 🧵 threads, custom booster roles, voice-channel text cleanup, and member activity tracking / inactivity roles.

## Commands

There is no test suite, linter, or `requirements.txt` — dependencies are installed by the `pip install -U discord.py openai` line in the `Dockerfile`.

```bash
# Run
docker compose up -d --build
docker compose logs -f

# Syntax check (no dependencies needed)
python -m py_compile bot.py activity.py describe.py

# Import smoke test — catches bad command decorators, duplicate command names,
# and import-time config errors. Needs discord.py + openai in the environment.
DISCORD_TOKEN=x OPENAI_API_KEY=x MONITORED_GUILDS='{"1":[2]}' \
  python -c "import bot; print(sorted(c.name for c in bot.client.tree.get_commands()))"

# Image titling in isolation
OPENAI_API_KEY=... python describe.py <image_url_or_path>
```

For behavioural checks, stub `discord.Message` / `discord.Member` with `types.SimpleNamespace` and call the `activity.handle_*` coroutines directly under `asyncio.run` — they take plain objects and need no gateway connection. Point `ACTIVITY_DB_FILE` at a temp path first.

## Architecture

### One client, one handler per event

discord.py allows only **one** `@client.event` per event name; a second silently replaces the first. `bot.py` owns the client and every event handler. `activity.py` deliberately registers **no** events — `bot.py` forwards into it (`activity.handle_message`, `handle_raw_reaction_add`, `handle_member_ban`, `handle_member_remove`, `handle_voice_state_update`, `activity.on_ready`).

Add new activity behaviour by forwarding from `bot.py`. Never decorate in `activity.py`.

`handle_message` returns `bool`: `True` means the message was in the inactive-only channel and `bot.py` must stop processing it.

### Command registration

`bot.py`'s `setup_hook` copies global commands into each guild in `MONITORED_GUILDS` and syncs per-guild (instant propagation), then clears the global set so commands don't appear twice. Global syncs take up to an hour — don't switch back to them.

`activity.py`'s commands are module-level `@app_commands.command` objects listed in `activity.COMMANDS`, attached by `activity.setup(client)`. That call sits right after `client = RoleManagerBot()` and must run before `setup_hook`, or the commands won't sync. This indirection exists so `activity.py` never imports `client` from `bot.py` (circular import).

### Persistence — the no-await invariant

Three JSON files under `/app/data` (mounted from `./data`): `activity.json`, `booster_roles.json`, `voice_warned_users.json`. No database, no locking.

Every read-modify-write of the activity DB is `load_db()` → mutate → `save_db()`. **There must be no `await` between the load and the save** — asyncio won't interleave an awaitless block, but any suspension point lets a concurrent event handler's write get clobbered by the stale snapshot.

Long-running operations (`catch_up`, `mark_inactive_members`) therefore buffer their findings in local lists/dicts during the awaiting phase and apply them all in one awaitless critical section at the end. Preserve that shape when editing them.

### Configuration

Env vars only, parsed at **import time** in `bot.py` and `activity.py`. A malformed or missing required var is an immediate crash on container start, not a runtime surprise. `DISCORD_TOKEN` and `MONITORED_GUILDS` are required; everything else has a default or is optional (an unset ID disables its feature).

`activity.ADMIN_ROLE_ID` falls back to `MODERATOR_ROLE_ID`, and server administrators always pass `is_admin`.

### Two independent role systems

Booster roles (`bot.py`, keyed in `booster_roles.json`) and the inactivity role (`activity.py`, `stripped_role_ids` in `activity.json`) both add and remove roles, and both persist state to survive a role being deleted out from under them. They don't coordinate — check both when debugging unexpected role changes.

## Gotchas

- **New `.py` files need a `Dockerfile` `COPY` line.** Only the listed files are copied into the image; a missing one fails at container start, not build.
- `get_image_title` is a **synchronous** OpenAI call made from inside the async `on_raw_reaction_add` handler. It blocks the event loop for the duration of the request.
- A thread created via `Message.create_thread()` shares its ID with the source message. `on_raw_message_delete` relies on this to `fetch_channel(payload.message_id)` — scanning `active_threads()` instead would miss auto-archived threads.
- Use `on_raw_*` events, not their cached equivalents. Reactions on messages predating the last restart aren't in the message cache and are silently dropped by `on_reaction_add`.
- `VOICE_TEXT_CHANNELS` holds **voice channel** IDs (Discord's built-in "Open Chat" sidebar), not separate text channels.
- `catch_up` re-counts messages already recorded live, so restarting within `CATCHUP_HOURS` inflates message/reaction totals. Last-seen timestamps and inactivity tagging are unaffected.
- `README.md` and the comments in `docker-compose.yaml` both document the full env var set. Update both when adding config.
