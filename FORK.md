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

The Kickstarter tier codes and their contents are listed in the
[README](README.md#kickstarter-tier-codes). Bundles are data in
[redeem_bundles.json](redeem_bundles.json).

## Server perks

[server_perks.json](server_perks.json) holds optional economy perks for a solo
or collection server. They are on by default; set a switch to `false` or `0`
to restore the original economy.

| Setting | Effect |
|---|---|
| `gold_rewards_also_grant_platinum` | Every gold reward also grants the same amount of platinum: campaign rewards, Frost Ring Arena cash-out, Wheel of Fate gold reels and redeem codes. |
| `level_up_currency_per_level` | Reaching account level N grants N × this value in gold and in platinum. Levels reached earlier are not paid out. |
| `wheel_paid_respin_refund` | A Wheel of Fate spin that leaves a paid re-spin also grants that spin's gold cost, at the chest's rarity after any upgrade. |

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
