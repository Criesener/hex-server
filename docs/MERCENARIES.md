# Mercenaries

Mercenaries are alternate PvE champions: fixed health, champion abilities, a
party passive, and their own deck. A campaign champion carries a party of
mercenaries and can fight an encounter as one of them instead of as itself.
This document describes the client contract (from the shipped client DLLs and
gamedata) and what the server implements.

Code: `services/mercenaries.py`, `services/deck_templates.py`, the
`partysave`/`partyload` handlers and mercenary battle selection in
`campaign.py`, and the login pushes in `application/profile_stream.py`.
Tests: `tests/tests_mercenaries.py`.

## Data

| Record | Meaning |
|---|---|
| `InventoryItemData` / `InventoryMercenaryData` (86) | The owned item. `m_MercenaryTemplateId` points to the template. The client identifies a party member by this **item** GUID. |
| `MercenaryTemplate` (166) | A `ChampionTemplate` subclass (`m_ChampionType = "Mercenary"`): `m_StartingHealth`, `m_ChampionAbilities`, `m_StartingHandSize`, `m_PartyPassiveAbilities`, `m_DeckRestrictions`, `m_UpgradeTemplateId`. |

`gamedata_seed._extract_champions` now reads `MercenaryTemplate` alongside
`ChampionTemplate` and treats mercenaries like PvP champions, so they appear in
`champion_templates_extended`, `champion_template_data`, `champion_abilities`,
`ability_effects`, and `card_abilities_meta`. `static.ensure_schema` backfills
these rows into databases seeded before this change. One mercenary, Andres
the Supremo, references abilities that are missing from the client's
`AbilityTemplate` records; those abilities are skipped.

## Party slots: `CAMP_PARTYCAP`

`PlayerProfile.GetUnlockedPartySlots()` returns the `Progress` of the
`CAMP_PARTYCAP` profile flag plus champion-talent bonuses
(`PartySizeBonusTalents`). `UIPartyMercenarySelector.GetMaxPartyForRace`
caps the party at 4 for Humans and 3 otherwise. With no flag the client shows
no usable party slots, which is why mercenaries were unreachable before.

Profile flags reach the client in the login profile stream as a standalone
`List<Reckoning.Profile.Messages.FlagData>` (`Name`, `Progress`, `Maximum`,
`Completed`). They are stored in `reckoning_flags` and only read at login, so a
changed cap applies after the next login.

The server derives the cap at every login (`mercenaries.sync_party_cap`): one
slot per distinct mercenary (`InventoryMercenaryData` item) the player owns,
at most 4 (the flag's Maximum; the client adds one slot per point without
capping it, then caps by race). A player without mercenaries and without a
flag gets no flag. `!partycap <0-4>` stores an override in
`party_cap_overrides` that wins over the derived value; `!partycap auto`
removes it. This follows the HEX Revival mod (`Mercenaries.RefreshPartyCap`:
mercenaries earned, max 4).

### How slots are unlocked (not implemented)

Retail unlocked mercenary slots through campaign quests. The client data
contains two AZ2 recruitment encounters, and both mercenaries are tagged
"AZ2 Starter Mercenary", but it has **no reward records** for them; the
unlock happened on the retail servers:

| Encounter (SceneData) | Location | Recruit (InventoryMercenaryData) |
|---|---|---|
| `b4d46c49-02d3-4df1-933d-6a265845124f` | AZ2 Node 9, Gaaffaa Bluff | Katsuhiro `540d0988-085c-4d62-8eb6-16361d1b3612` |
| `537f9890-1d2f-42ed-94c5-cd1f1eef589f` | AZ2 Node 30, Cliffs of Nore | Augustine `1da0e36f-cb71-432d-a282-8c4a2a01c0e2` |

Earning one of these recruits raises the cap through the owned-mercenary
count above. Open questions: only two recruitment fights exist in the client data while
three slots are possible, so the third unlock is unknown, and whether the
recruit item itself was granted is inferred from the "you're hired"
conversations.

## Party persistence

Party I/O uses the campaign service (`ServiceCampaign`, dt 110000) JSON
envelopes:

- `partysave` — `{"Party": ChampionParty}`; the response envelope is the saved
  `ChampionParty` JSON (the client deserializes it and calls back with it).
- `partyload` — `{"PlayerId", "Champs": [ids]}`; answered with the matching
  parties.

`ChampionParty` is `{Id, ChampionID, PlayerId, Members: [{Mercenary:
{"m_Guid": item guid}, DeckTemplate: <profile deck template id>, Upgrade}]}`.
`ChampionID` is the champion's database id. Parties are stored in
`champion_parties` and also sent at login as
`List<Game.Shared.Campaign.Messages.CampSysGeneral+Party+ChampionParty>`.

## Mercenary decks: profile deck templates

A mercenary's deck is a `ProfileDeckTemplate`, saved through the profile
service's JSON `Network+Request` (dt 80000):

- `{"action": "pdecktsave", "Template": base64(ProfileDeckTemplate.ToBytes()),
  "DeckTemplateID": 0 or existing id}`
- `{"action": "pdeckdel", "DeckTemplateID": id}`

The **response envelope is ObjFmt, not JSON**: the client decodes it with
`EncData.Decode` and expects a `Game.Shared.Profile.SavedProfileDeckTemplate`
(`Id`, `Name`, `Comp`, `Data`). Answering `{}` made the client throw and the
save silently failed. Templates are stored in `profile_deck_templates` and
sent at login as `List<SavedProfileDeckTemplate>`.

`ProfileDeckTemplate` bytes (also used by `EncodedDecks`): name, champion
GUID, sleeve GUID, `Equip` (`varint count`, then `varint EEquipmentType` +
16-byte GUID each), then cards (GUID, `varint` count, reserve/extended/foil
bytes, gem list). `deck_templates.template_cards` parses the card list.

## Fighting as a mercenary

The campaign `start` SendEvent carries the choice as
`OParms: [..., "merc=<item guid>"]` (the zero GUID means the champion itself).
`JoinSession` still reports the champion's own deck, so the server remembers
the choice per campaign (`campaign._active_mercenary`) and
`resolve_battle_config` substitutes the mercenary's template, name, starting
health, no talents, and the party member's saved deck template.

### Mercenary equipment

The party deck template's `Equip` slots (`deck_templates.template_equipment`,
in `EEquipmentType` order Head, Chest, Gloves, Feet, Weapon, Trinket) are the
mercenary's battle equipment, like HEX Revival's `Decks.FromTemplate`. Items
the player does not own and a second item of one type are dropped
(`equipment.sanitize_deck_equipment`). Battle setup then uses the same path
as a champion deck: cards are swapped for their equipment-modified variants
(`equipped_variant`) and the battle HUD gets `EquipmentSet`.

## Party passives

Every `MercenaryTemplate` has `m_PartyPassiveAbilities` (almost all "At the
start of the game" / "Before the start of the game" triggers). In the
authoritative session (`AuthoritativeSessionBase.LoadPlayerDeck`), the game
server gets `DeckChampInfo.PartyMembers` — the party's MercenaryTemplate ids,
each followed `Upgrade` times through `m_UpgradeTemplateId`
(`MercenaryTemplate.GetUpgradeTemplate`) — and
`InstantiateChampionCard`/`InstantiateMercenaryCard` add every member's party
abilities to the player's hidden simulacrum card, for the champion and for a
fighting mercenary alike.

`DeckChampInfo` only travels inside `PlayerDeckChampInfo` in
`StartEncounterRequestArgs`, a campaign-server-to-game-server request; the
client never sends or receives it (no client code builds the request, and
`StandaloneSession.HasDeckAndChampInfo` returns `partyMembers = null`). Our
server is the game server, so it applies the passives itself:

- `mercenaries.party_member_templates` is our `PartyMembers`;
  `party_passive_ability_guids` flattens their party abilities (skipping
  abilities missing from the client data). The MercenaryTemplate records are
  read from `Records/MercenaryTemplate.jsonl` with the seed parser, because
  the strict `RecordStore` rejects their unquoted `m_ShardAlignment` keys.
- `resolve_battle_config` returns `party_members` and
  `party_passive_abilities` for the campaign's champion.
- Battle setup appends the passives to the fighter's champion ability list
  (`_player_champ_abilities`, after its own powers). The RulesPort discovers
  their PreGame/GameStarted triggers from that list, like talent triggers,
  and the client sees them in the champion's `CardUpdated` ability list.

## Testing

Developer commands (require `HEX_PROFILE_FLAGS=allowcon`):

- `!additem <name>` — grant any inventory item, e.g. `!additem B.E.B.O.`
- `!partycap <0-4>` — override `CAMP_PARTYCAP` (applies after relogin);
  `!partycap auto` returns it to the owned-mercenary count

## Not implemented

- Granting the AZ2 recruits themselves (see above).
- Upgrades: the profile `mercupd` action (`ProfileMercActionReq`:
  `MercID`, `ChallengeSuccess`, `PayGoldAmount`), the login
  `List<MercLeveling>`, and dungeon mercenary nodes (`/merc_node_ch`,
  `/passivemercs` campaign flags). Players reported upgrading a mercenary by
  winning two dungeon merc encounters with it and paying 25,000 gold.
- A live `CAMP_PARTYCAP` update without relogging (the client has a
  `UserFlagsUpdated` event, not yet used).
- Known ability issues are tracked in `PRIVATE_SERVER_FEATURES.md`.
