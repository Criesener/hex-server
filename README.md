# Hex TCG Private Server

> **Fork notice:** this is a fork of [MTGQuarters/hex-server](https://github.com/MTGQuarters/hex-server)
> with additional fixes and macOS tooling. See [FORK.md](FORK.md).

This repository builds an authoritative private server for **HEX: Shards of
Fate**. It allows an original game client to connect to a locally controlled
or privately deployed backend while the server owns the game session,
profiles, card collection, rules, card abilities, AI, and persistence.

The server is designed to run separately from the game client and can be hosted
on another machine for multiplayer sessions. A player with the original game
installed only needs to change the client configuration to point at the server.

This project was developed with assistance from AI tools such as Codex and
ChatGPT. Some of the documentation was also generated with AI assistance as a
reference for development and debugging.

Start with:

- [docs/PRIVATE_SERVER_FEATURES.md](docs/PRIVATE_SERVER_FEATURES.md) for the
  implemented/missing feature checklist.
- [docs/CLIENT_SERVER_PROTOCOL.md](docs/CLIENT_SERVER_PROTOCOL.md) for the
  protocol, card zones, event ordering, and gamedata pipeline.
- [HOWTO.md](HOWTO.md) for the compact implementation, protocol, database,
  and operations specification.
- [RULES.md](RULES.md) for the canonical gameplay contract and known parity
  gaps.
- [docs/COMMANDS.md](docs/COMMANDS.md) for the supported in-game debug
  commands.
- [docs/FEATURE-FLAGS.md](docs/FEATURE-FLAGS.md) for server-configured client
  profile features such as the developer console and replay UI.
- [abilities/README.md](abilities/README.md) for the gamedata-driven ability
  framework and custom ability extension point.

The smaller package READMEs describe individual extension points:
`domain/`, `services/`, `gamemodes/`, and `campaign_chains/`.

## What is an “authoritative server”?

In a server-authoritative game, clients request actions and display the
result. They do not decide the result. For example, when a player plays a
troop, the server validates the phase and priority, checks the card's current
zone, pays the cost, resolves abilities and triggers, applies combat/state
rules, moves cards between zones, and publishes the resulting events. Both
players receive a consistent projection of that server state, with hidden
information filtered appropriately.

This also makes automated testing possible: a test can set up a board, perform
an action, and check that the result is correct without opening the game
client. The client's rendering and animation are separate from the server's
game decisions.

## Why Python instead of C# or reusing the original DLLs

The original game client is written in C#, so reusing its DLLs might seem like
the simplest approach. However, those files were designed to run the game
client, not a standalone server. They expect client-specific systems such as
graphics, menus, local sessions, and the original game services.

Python is a better fit for the server because it is easier to read, modify,
test, and deploy. It also makes tasks such as managing player accounts,
collections, campaigns, databases, AI opponents, and multiple connected
players straightforward.

The server still uses the original client as an important reference. We use its
game data and observed behaviour to reproduce card rules and client
compatibility, while keeping the actual game state and decisions on the server.

## Current release

This repository is being prepared as version **0.4.2**. It is an early,
compatibility-focused private-server release: the feature checklist records
what has been tested and the remaining parity gaps. See
[CHANGELOG.md](CHANGELOG.md) for the release summary and
[docs/RELEASING.md](docs/RELEASING.md) for the GitHub/GHCR release procedure.

## Kickstarter tier codes

The original Kickstarter codes were checked against Cryptozoic's servers and
can no longer be verified. Instead, each 2013 pledge tier has its own code for
the client's Store → Redeem Code dialog. Each player can redeem each code once.
Every tier includes the tier below it, and every tier from Squire up adds the
Million Dollar Deck Sleeve stretch-goal reward.

To redeem a code, open the client's **Store → Redeem Code** dialog and enter
it, for example `KS-WARRIOR`. Cards, decks and packs arrive at once; very
large grants such as all equipment (`KS-IMMORTAL`, `KS-PRODUCER`) appear after
the next login.

| Code | Tier | In-game contents |
| --- | --- | --- |
| `KS-SUPPORTER` | Supporter | nothing in-game (beta invite) |
| `KS-SQUIRE` | Squire | 10 Shards of Fate packs, 2 starter decks, Lady Cassandra |
| `KS-WARRIOR` | Warrior | 25 packs, 3 decks, AA Replicator's Gambit, Zoltog, Slithering Marauder, Warrior Sleeve |
| `KS-CAPTAIN` | Captain | 40 packs, 4 decks, AA Princess Victoria, Burgamot Ebonrock, Prospero, Captain Sleeve |
| `KS-KNIGHT` | Knight | 60 packs, AA Extinction, Mooof, Darad, Knight Sleeve |
| `KS-CHAMPION` | Champion | 85 packs, AA Ragefire, Mephistophelius, The Wrath of Zakiir, Champion Sleeve |
| `KS-KING` | King | 150 packs, AA Pack Raptor, Monkey of the Nine Tails, Spectral Oak, King Sleeve |
| `KS-COLLECTOR` | Collector | King + a second copy of each exclusive card, Collector Sleeve |
| `KS-RAIDLEADER` | Raid Leader | King + Raid Leader Sleeve |
| `KS-DUNGEONCRAWLER` | Dungeon Crawler | King + 20 random Rare/Legendary equipment, sleeve |
| `KS-GUILDMASTER` | Guild Master | King + sleeve; friends redeem `KS-GUILDGIFT` for 30 packs |
| `KS-PROPLAYER` | Pro Player | King + Pro-Player Sleeve |
| `KS-GRANDKING` | Grand King | all six $250 tiers (King once) + sleeve |
| `KS-DRAGONLORD` | Dragon Lord | Grand King + 1 of every PvP card + sleeve |
| `KS-PRIMAL` | Primal | Grand King + 4 of every PvP card + sleeve |
| `KS-IMMORTAL` | Immortal | Primal + all equipment + sleeve |
| `KS-PRODUCER` | Producer | Immortal + Producer Sleeve |
| `KS-EXTRA-MERCS` | – | the Kickstarter mercenaries outside the tiers: Aethynia, B.E.B.O., Cardboard Tube Samurai, Glorfenblort |

Some Kickstarter cards were renamed before release: Void Marauder became
Slithering Marauder, Scourge Knight became Darad, the Scourgeblade, and
Spectral Lotus Garden became Spectral Oak. Lifetime perks such as extra loot,
an extra card in the opening hand, weekly drafts, guild XP and VIP are not
modeled.

Bundles are data: edit [redeem_bundles.json](redeem_bundles.json) to change
the contents or add codes. No code change is needed.

## Running locally via Docker

You do not need to download this repository to run the server locally. You can
use the published Docker image instead.

Docker runs the Hex server in a self-contained container. This means you do not
need to install Python or configure the server components manually.

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/) using
the official Docker instructions. Docker Desktop is available for Windows,
macOS, and Linux.

You must have HEX: Shards of Fate installed because the server uses the game's
`Data/gamedata` file to create its card and ability data.

The Docker setup:

  - Runs the game server and web proxy together.
  - Makes the server available on ports 9933 and 8081.
  - Keeps your database and player data in a separate folder so they survive container updates.
  - Allows the server to run on Windows, Linux, or macOS through Docker Desktop.

The commands below are for Windows PowerShell. Linux and macOS users will need
to use equivalent paths and shell commands for their systems.

## Starting the server
Once Docker Desktop is installed, open PowerShell and run the following
commands. Update `$ClientData` if the game is installed somewhere else.

```powershell
$HexState = Join-Path $HOME 'HexServer'
$ClientData = 'C:\Program Files (x86)\Steam\steamapps\common\HEX SHARDS OF FATE\Data'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -p 9933:9933 -p 8081:8081 `
  --user 1001:1001 `
  -v "${HexState}:/hex/state" `
  -v "${ClientData}:/client-data:ro" `
  -e HEX_DB_PATH='/hex/state/hconnect.db' `
  -e HEX_GAMEDATA='/client-data/gamedata' `
  ghcr.io/ianutley/hex-server:0.4.2
```

```bash
HEX_STATE=$HOME/HexServer
CLIENT_DATA='location of Hex Client Data directory'

mkdir -p $HEX_STATE
# The container runs as UID 1001 and must be able to traverse and write here.
chmod 777 -- "$HEX_STATE"

docker run --rm \
  -p 9933:9933 -p 8081:8081 \
  --user 1001:1001 \
  -v "${HEX_STATE}:/hex/state" \
  -v "${CLIENT_DATA}:/client-data:ro" \
  -e HEX_DB_PATH='/hex/state/hconnect.db' \
  -e HEX_GAMEDATA='/client-data/gamedata' \
  ghcr.io/ianutley/hex-server:0.4.2
```

You can try using the `latest` tag to be on the bleeding edge.

Every container start applies the current schema and re-syncs the server's card,
ability, champion and encounter data from the mounted `gamedata`, in place, so
an image update does not cost you your progress. Watch the container log for:

  - `[docker] applying current schema and seeds to ...` — always printed for an
    existing database.
  - `Refreshed client-derived rows for ...` — the mounted game data was newer
    than the database, which has now been updated.
  - `Client-derived row refresh skipped (...); keeping the existing client data`
    — no game data was mounted, so the database kept its old card data. Check
    the `HEX_GAMEDATA` volume before playing.

The first time the data is refreshed, the database is copied once to
`hconnect.db.preseed.bak` next to it in `$HEX_STATE`, so a bad update can be
rolled back by replacing `hconnect.db` with that file while the container is
stopped.

After starting the container, configure the Hex client to connect to the
computer running the server.

## Pointing a Windows client at the server

For a local Docker server on the same Windows PC, use Command Prompt and the
default Steam installation path:

```cmd
set "HEX_GAME=C:\Program Files (x86)\Steam\steamapps\common\HEX SHARDS OF FATE"
copy "%HEX_GAME%\config.ini" "%HEX_GAME%\config.ini.backup"
notepad "%HEX_GAME%\config.ini"
```

Update the existing values under `[SystemSettings]` to:

```ini
[SystemSettings]
GameServerIP = 127.0.0.1:9933
CZEAuthUrl = http://127.0.0.1:8081/auth/hexlogin
CZEPayUrl = http://127.0.0.1:8081/auth/hexpaylinkses
NewsEventsURL = http://127.0.0.1:8081/NewsEvents/NewsEvents-{0}.txt
```

If Windows prevents saving under `Program Files (x86)`, start Command Prompt
as Administrator before running these commands. Fully restart the client after
saving. `127.0.0.1` works because both the client and Docker-published ports
are on the same Windows machine.


### Authentication and two-client testing

The proxy supports non-Steam login through `/auth/hexlogin`. For local PvP
testing, use two distinct non-Steam account records and the same server
address in both clients. This avoids Steam's single-instance and ticket
authentication behavior. Steam configuration remains available when Steam
authentication itself is the behavior under test.

## Architecture at a glance

`hconnect_server.py` serves the TCP HConnect protocol on port 9933.
`proxy.py` serves HTTP compatibility/auth and news endpoints on port 8081.
`db.py` owns the shared SQLite connection while `profile_db.py`, `pve_db.py`,
`pvp_db.py`, `tournament_db.py`, and `replay_db.py` expose domain persistence
APIs. `static.py` owns schema creation and server-owned seeds;
`battle_engine.py` owns turn/priority rules; `game_engine.py` serializes session
events; and the `abilities/` framework resolves gamedata-driven effects.

## Database capacity note

SQLite is the default development database. Before running tournaments at
32-player scale (up to 16 simultaneous games), evaluate upgrading the server
to PostgreSQL rather than relying on SQLite. SQLite WAL permits concurrent
reads but still has one database-wide writer, and the game engine frequently
updates persisted `game_cards` state. PostgreSQL should be introduced with a
connection pool and short transactions; changing `HEX_DB_PATH` alone is not a
drop-in migration.
