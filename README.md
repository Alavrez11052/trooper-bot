# FLPD Discord Bot

A Discord bot made for FLPD to handle duty time, moderation, and disciplinary actions.

## Features

* **Duty Clock** - Clock In, Clock Out, My Time, Request Time Fix, and a leaderboard.
* **Time Fixes** - Officers can request corrections to their hours, and supervisors can approve or deny them.
* **Leaderboard** - Shows the top 10 officers by hours, with options for current and all-time hours.
* **Monthly Hours Reset** - Resets the current hour count without removing all-time hours.
* **Moderation** - Kick, ban, unban, timeout, untimeout, purge, slowmode, lock, and unlock.
* **Discipline** - Warnings, strikes, suspensions, terminations, blacklists, records, and voiding disciplinary actions.
* **Configurable Embeds** - Allows the bot's color, footer, icon, thumbnail, channels, and on-duty role to be changed.
* **Logging** - Keeps track of moderation, discipline, clock activity, and time-fix requests.
* **Automatic Suspensions** - Suspensions expire automatically and can remove the suspension role when finished.

## Commands

### Duty

* `/clockpanel`
* `/hours_clear`
* `/hours_adjust`

### Moderation

* `/kick`
* `/ban`
* `/unban`
* `/timeout`
* `/untimeout`
* `/purge`
* `/slowmode`
* `/lock`
* `/unlock`

### Discipline

* `/warn`
* `/strike`
* `/suspend`
* `/terminate`
* `/blacklist`
* `/record`
* `/void`

### Configuration

* `/config`
* `/embed`

## Permissions

The bot uses Discord's built-in permissions instead of having its own rank system.

| Action                           | Permission       |
| -------------------------------- | ---------------- |
| Clock features                   | Everyone         |
| Time-fix approvals               | Kick Members     |
| Warnings / Strikes / Suspensions | Kick Members     |
| Kick                             | Kick Members     |
| Ban / Unban                      | Ban Members      |
| Timeout / Untimeout              | Moderate Members |
| Purge                            | Manage Messages  |
| Slowmode / Lock / Unlock         | Manage Channels  |
| Terminate / Blacklist / Void     | Administrator    |
| Configuration                    | Manage Server    |

This makes it easy to match the bot's permissions to your department's rank structure.

## Data

The bot uses SQLite to store officer hours, disciplinary records, configuration, and other bot data.

The clock panel and other features are designed to keep working after bot restarts, and suspensions are automatically checked and expired.
