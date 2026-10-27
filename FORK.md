# About this fork

This is a fork of [MTGQuarters/hex-server](https://github.com/MTGQuarters/hex-server),
licensed under the GNU AGPL-3.0 like the original (see [LICENSE](LICENSE)).
It tracks the upstream `pve-equipment-and-mercenaries` branch and adds gameplay
and client-compatibility fixes found while playing the PvE campaign and the
Frost Ring Arena with the original macOS Steam client.

No game assets are included. You need your own copy of the HEX client, whose
`Data/gamedata` the server reads at startup.

## Branches

| Branch | Purpose |
| --- | --- |
| `develop` (default) | upstream `pve-equipment-and-mercenaries` + the fixes below; ongoing work happens here |
| `main` | mirror of upstream `main` |
| `fix/*` | single fixes cherry-picked onto upstream branches for pull requests |

Upstream changes are merged into `develop` from time to time.

## Changes on top of upstream

Campaign and arena
- Unpack all four columns of `db_latest_campaign_state` in the AZ1 transition (fixes the endless Crayburn conversation and disconnect)
- Push the champion's `LastDeckID` at login, so selecting a champion no longer opens the deck editor first (`HEX_CHAMPION_DECK_EDITOR=1` restores the old behavior)
- Close a Frost Ring Arena run on cash-out even without rewards
- Send the player's deck equipment with `EquipmentSet` at battle start

Collection and decks
- Only push unseen cards via `CardsAdded` (cards no longer show as "New" every time)
- Keep socketed gems in the client's new gem format (bit 62); trim gems to the card's socket count; drop `ActiveGems` for cards without sockets
- Handle `RemoveDeck` requests

Rules and battle flow
- Project the card state in attribute-grant `CardUpdated` events (attackers no longer untap and re-tap)
- Defer the AI's coin-toss pick until the coin animation has played (`HEX_AI_COIN_PICK_DELAY`, seconds, default 7)
- Keep a resource's threshold picker open while a charge trigger waits
- Give priority in skipped windows while the chain is not empty
- Send all target slots of a card ability in one `OptionInstance` (e.g. Boulder Toss)
- Grant a resource's authored charge amount (e.g. Crackling Vortex)
- Feed the trigger event TAC to deck-search filters (e.g. Brutal Commander)
- Keep an attack trigger's target prompt open (e.g. Veteran Gladiator)
- Resume a paused combat after a Deathcry discard (e.g. Bloatcap)
- Treat Scrounge as an activated ability, not a static bonus; AI gem handling

Rewards
- Kickstarter tier redeem codes (see below)
- Spectral Oak creates a Spectral Acorn on the first login of each day (UTC)

Operations
- `HEX_TRACE_EVENTS=1` logs every game event pushed to the client
- Login passwords and tokens are masked in the request log

## Kickstarter tier codes

The original Kickstarter codes were checked against Cryptozoic's servers and
can no longer be verified. Instead, each 2013 pledge tier has its own code for
the client's Store → Redeem Code dialog. Each player can redeem each code once.
Every tier includes the tier below it, and every tier from Squire up adds the
Million Dollar Deck Sleeve stretch-goal reward.

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

## Running on macOS

[scripts/macos/hex-server.sh](scripts/macos/hex-server.sh) wraps the Docker
container and switches the Mac client's `config.ini` between this server and
the original one:

```sh
docker build -f docker/Dockerfile -t hex-server:local .
export HEX_APP="/path/to/HEX SHARDS OF FATE/Hex.app"   # if not in Steam's default library
scripts/macos/hex-server.sh create          # first run
scripts/macos/hex-server.sh client-local    # point the client at 127.0.0.1
scripts/macos/hex-server.sh logs
```

`scripts/macos/hex-server.sh client-original` restores the client's original configuration.
