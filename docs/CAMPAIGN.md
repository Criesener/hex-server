# Campaign Architecture

This document describes the campaign model used by the private server and the
client-facing Hex campaign protocol. The authoritative server implementation is
`campaign.py`; the persistent schema is declared in `static.py` and the live
state is stored in the `campaigns` table.

## The four campaign types

`campaigns.campaign_type` is serialized to the client as the `CampType` enum:

| Type | Enum value | Purpose in this server |
|---|---:|---|
| `PANORAMA` | 6 | Race-specific starter area and post-dungeon NPC conversations |
| `QUEST` | 4 | Journal entry and its progressive objectives |
| `AREA` | 2 | AZ1/AZ2 overworld map with connected nodes |
| `DUNGEON` | 1 | A dungeon map such as Crayburn Castle |

The protocol also has `WORLD`, `ACHIEVE`, `STRONGHOLD`, and `TEST` enum values,
but they are not full campaign implementations here.

`campaigns.id` is the server's campaign ID. It is independent of the
`template_name` (for example `AZ1`, `Crayburn Castle`, or `az01_tamed`) and of
the champion ID. A campaign row contains the champion owner, campaign type,
start flag, and a JSON `GameplayState` snapshot.

## Lifecycle

The normal client flow is:

1. `qcur4champ` finds the champion's current campaign. An unfinished dungeon
   is preferred, then an unfinished Crayburn journal quest, then the panorama.
2. `getcampsum` returns the `CampSummary` and the Unity resource paths for the
   selected campaign type.
3. `getcampstate` returns the full `GameplayState`.
4. `startcamp` marks the campaign started. For Crayburn it exposes the first
   conversation node.
5. The client sends `locaction`/`StartLoc` when a map location is selected and
   `sendevent` events such as `visit_path`, `conv_done`, `empty_done`, or
   `start` as the location is resolved.
6. An encounter sends `gamestarted`, creates a game session, and runs through
   the normal battle protocol.
7. The battle result calls the campaign game-end path. The server updates the
   campaign state, applies authored rewards, and sends `gameendnotify` with
   `Applied` updates. The client caches those updates and opens its campaign
   loot window.

The client renders a campaign from three related state collections:

```text
GameplayState
├── LocNodes       map/prefab node IDs (Node001, Node002, ...)
├── VisLocs        Location data and progress flags
├── Encounters     scene GUID catalog used by Location.Encounter
└── PublicState    visited nodes/paths, quest markers, and campaign flags
```

`Location.Node` must match a `LocNodes` node name. `Location.Name` is the
user-facing name sent in `StartLoc`. `Location.Encounter` is a scene GUID,
while `Location.ConversationID` is a conversation-template GUID.

## PANORAMA

The panorama is a race-specific NPC scene, not an overworld map. It is used for
the tutorial chain:

```text
intro NPC conversation
        ↓
trainer conversation → training encounter
        ↓
training victory conversation
        ↓
quest-giver conversation → Crayburn Castle
```

The race configuration in `campaign.py` supplies the NPC names, conversation
GUIDs, trainer champion GUID, training encounter, and race-specific prefab.
Training battle completion sets `TutorialDone`, hides the trainer, and reveals
the quest-giver. Training defeat conversations leave the training encounter
retryable.

After Crayburn is complete, the panorama is also used for the report-success
conversation and the race NPC's AZ1 travel conversation. Completing the travel
choice activates the `AREA` campaign at `Into The Woods`.

## AREA

An area is an overworld map. AZ1 uses the Howling Plains `SceneData` record and
the client assets:

```text
BackgroundPrefab: campaign/azmap/azmap
NodesPrefab:      campaign/az01/nodes
```

The authored `SceneData.m_ItemData` supplies map-node IDs, titles, descriptions,
terrain types, and node artwork. The server converts those records into
`VisLocs` and `LocNodes`. The client NodesPrefab supplies the visual paths; the
server-side `campaign_node_edges` table stores their adjacency for movement
validation and visibility. Visibility and enabled flags control which
locations are selectable; `PublicState.Data.visited_paths` records the path
identifiers reported by the client, while `locked_paths` suppresses an
individual path even when both endpoint nodes are visible. Fog of war is
therefore a client rendering effect driven by the server's visible nodes,
visited paths, and locked paths.

For AZ1, the initial state exposes `Node001` and its adjacent `Node002`.
Completing a node, or returning from its encounter in a retryable state,
reveals its graph neighbours. Arrival at an unfinished encounter does not
draw its outgoing paths. An alternate route may reveal a destination node, but
the path from an unfinished node to an unvisited destination remains locked
with `PublicState.Data.locked_paths`. The opening topology includes the direct `Node003`
→ `Node007` route (Dunnwood to The Road of Oaks), which is not represented by
`SceneData` list order:

```text
Node001  Into The Woods
   ↓
Node002  North Feralroot Woods (faction conversation)
   ↓
Node003  Dunnwood (Wild Cub / taming encounter)
   ↓
Node004  Sporemist Hollow (Shroom Haus card choice)
```

The server resolves AZ1 scene records by matching the authored node number to
the `encounter_scenes.name` (`AZ 1 - NODE ...`). This keeps the node map data,
scene GUID, AI deck, opponent champion, abilities/modifiers, and rewards in
separate data tables.

`campaign_node_conversations` is the corresponding catalog for authored AZ1
and AZ2 conversation templates. It is populated from the `AZ1/AZ2 - Node ...`
names in `ConversationTemplate.jsonl` and stores the normalized node ID,
conversation GUID/name, and a small trigger descriptor (first/repeat,
success/fail where the authored name makes that distinction). It is separate
from `conversation_rewards`: the latter records what a completed conversation
grants, while this table records which conversation can be triggered at a
node.

The checked-in `SceneData` records contain node metadata but do not contain the
map path graph. The `campaign_node_edges` seed is extracted from the client
`Nodes`/`Paths` prefab roots in `Hex_Data/resources.assets`: 80 AZ1 paths and
94 AZ2 paths, stored bidirectionally. Deriving edges from `m_ItemData` order
would create false routes.

### AREA progress rules

- A conversation node completes on `conv_done`.
- An empty node completes on `empty_done`.
- An encounter node completes only through the battle game-end path.
- A completed non-repeatable encounter cannot be started again.
- A conditional encounter (currently taming) remains visible, enabled, and
  repeatable when the battle is won without satisfying its condition.
- The server must not mark an unfinished encounter complete merely because the
  client attempts to travel away from it.

Node002's conversation grants the `az01_tamed` quest and the faction quest:

| Faction | Quest script |
|---|---|
| Ardent (Human, Elf, Coyotle, Orc) | `az01_ar_find_ambling_mesa` |
| Underworld (Vennen, Necrotic, Shin'hare, Dwarf) | `az01_uw_find_cave_in` |

New quest campaigns are sent with `CampSpawnNotify` (`campspawn`). A normal
`cmpupdate` updates an existing cached campaign but does not create a new quest
entry in the client's journal.

### AZ1 observed quest state machine

The AZ1 route reviewed against both the Ardent and Underworld playthroughs is
represented by the same persisted node/quest state rather than by a separate
client map model. Faction- and race-specific authored conversation variants
are selected from `quest_conversations` for the champion that owns the quest:

| State transition | Server result |
|---|---|
| Node012/Winston starts `Cross the Zila River` | reveal and mark Node013/Node014; keep the west Zila bridge gated |
| Node014 prelude → Savage Lord → success | complete the encounter, advance Step1 (`Razortooth Forest`), then expose the Winston objective |
| Winston objective → Weston objective | select the authored conversation on the existing node and keep the node open until its turn-in completes |
| Node019 blockade conversation | leave Brink Ridge incomplete and hide its northbound destinations |
| `q_smoldering_dead` starts | replace the Brink Ridge explanation with its authored encounter; only a successful result clears the blockade |
| Wallace starts `Cross the Zodiac River` | reveal Shadowgrove and mark it; keep the west Zodiac bridge hidden |
| Corrupt Dryad success → Wallace turn-in | advance the quest and unlock the west Zodiac bridge |
| Warren completion | finish the Cross Zodiac quest and grant its authored card and pack |
| Vale of Oberon with Fort Romor/Usurper | select the authored faction-quest conversation, keep Node00R retryable, and reveal/enable Lena Grotto; the finished-quest branch completes Node00R when closed |

The legacy Cross-Zila objective omitted its encounter GUID in the source
`QuestTemplate`. `static.ensure_schema` repairs it to the Node014 Savage Lord
scene (`ab77df1e-5f13-471b-80e7-b7b4824ca280`) so quest markers, retry rules,
journal advancement, and battle rewards share one identity. Encounter and
conversation rewards remain in their authored `rewards_json` and
`conversation_rewards` records; for example, Gnash Bridges grants the
Ravenous Piranha card, Shadowgrove grants the Howling Plains pack plus its
success-conversation equipment, and the Zila/Zodiac turn-ins grant their pack,
equipment, or card rewards.

## DUNGEON

Crayburn Castle is represented as a `DUNGEON` campaign with the real castle
assets:

```text
BackgroundPrefab: campaign/az01/crayburncastle/prefabs/background
NodesPrefab:      campaign/az01/crayburncastle/prefabs/nodes
```

The castle route is an ordered chain:

```text
Entrance → WatchTower → Drawbridge → CastleGate →
InnerBailey → TowerGate → PenworthTower
```

The map still requires physical movement to the next node. The server does not
automatically move the avatar to a later node after a win. It reveals the next
node and waits for the client's path/travel request.

Each race has its own conversation and encounter mapping. A node can be:

- a conversation, which advances on `conv_done`;
- an encounter, which launches the scene attached to that node; or
- a pass-through node, which is completed immediately.

Encounter victories can queue race-specific success conversations, and losses
can queue race-specific defeat conversations. Completing the final castle
objective advances the linked Crayburn quest and exposes the report-success NPC
in the panorama.

### AZ1 DungeonTemplate dungeons

The other AZ1 dungeons use one generic runner that is driven by data.
Crayburn keeps its own seed and code path.

**Seed (`dungeon_templates`).** `AssetExtraction/gamedata_seed.py` resolves
every `DungeonTemplate` into ordered rooms. Each room has its map node and a
list of stages. A stage holds prelude conversations, an optional encounter,
and that encounter's Success and Fail conversations. The resolver first uses
the authored `m_EventsToFireWhenVisited`. Where those events are missing,
dangling, or shifted to the wrong room, it falls back to the client naming
convention: `<Dungeon> - <Room>[ Success| Fail][ - <Faction>]` for
conversations and `<Dungeon> <Room> …` for encounter scenes. Faction variants
are kept as `*_by_faction`. The seed also includes the scenes and decks that a
dungeon names or references. Dungeon scenes pay the AZ1 default of 100 gold
and 100 XP, because the records contain no reward data.

**Map.** The node paths come from the client prefab
`<root>/prefabs/nodes`, stored in `static.DUNGEON_PREFAB_PATHS` and seeded
into `campaign_node_edges` under the prefab root. Run
`AssetExtraction/extract_dungeon_prefabs.py` to regenerate them. A template is
bound to its prefab by name, in any word order and with or without "the".

**Entrance.** The overworld scene `AZ 1 - NODE nn - <Dungeon> - DUNGEON`
has no players. It is sent as a `Dungeon` portal with no encounter, and it
stays repeatable until the dungeon is finished. The client enters the dungeon
through StartLoc, or through `start` while the portal is the active location.
Both create or resume the champion's `DUNGEON` row, named after the template,
and push a `cmpupdate` transition. If the client cannot render a dungeon,
for example because its rooms are on nodes the prefab lacks (Fort Romor), the
entrance is a safe `Empty` location instead. AREA hydration never sends an
`Encounter` whose scene has no AI deck. A zero-GUID deck counts as no deck,
so Node046's Tanning Dog is a conversation. Deck-less scenes such as the
panoramas are also kept out of `GameplayState.Encounters`, because the node
info panel calls `GetChampionTemplate` for every repeatable location whose
encounter it can resolve.

**Run.** The run lives in the state under `DungeonRun`. The client locations
are recomputed from it on every response:

| Room position | Location sent |
|---|---|
| prelude conversation, or a queued Success/Fail conversation | `Convo` (auto-start), ends with `conv_done` |
| battle | `Encounter`, can be cancelled; `start` launches it |
| room without content | `Empty` (auto-start), completes on arrival |
| done, or the locked final room | completed `Empty` |

A node is visible when it is the start node, when it has been visited, or when
it is next to a visited node that has no unfinished content. Path forks
connect nodes but are never destinations. The final room, which is the last
room with content, opens only after every other room is done. Rooms that share
a node play in template order, so the Tranquil Dream hub comes before Sister
Midnight.

- **Won battle:** plays the Success conversation, then moves to the next stage.
- **Lost battle:** plays the Fail conversation, then leaves the battle
  retryable.
- **Finished final room:** advances the active `QuestObjectiveDungeon`, which
  is matched by template GUID or entrance scene, and returns to the AZ1 map at
  the entrance. The entrance is then shown as completed.
- **Leaving the dungeon** (forfeit) abandons the run and also returns to the
  entrance. Entering again starts a fresh run.
- **Quest granted after the dungeon is finished:** the new quest advances to
  match.

## Map-wide check

`campaign_map_check.py` checks that every campaign node works. It drives
`campaign.py` through its real request handlers and decodes the responses
the client would receive.

**Client contract.** These checks run on every location of every response:

| Location | Requirement |
|---|---|
| any | `node` is in `LocNodes`; `type` is a client `LocationType` name |
| `Encounter` | the scene is in `Encounters`, in `encounter_scenes`, and has an AI deck with cards and an AI champion (what `EncounterScene.GetChampionTemplate` needs) |
| `Convo` | `conversationId` is a known `ConversationTemplate` |
| `Panorama` | the scene exists |
| `Dungeon` (AREA) | a runnable DungeonTemplate; the only fallback is `Empty` with `dungeon_unavailable` |
| repeatable, with an encounter in `Encounters` | the scene has an AI deck: the node info panel calls `GetChampionTemplate` |
| dungeon terrain | never an `Encounter` |

For a map it also checks the paths:

- Every path endpoint of the client prefab (`campaign_node_edges`) has a
  location.
- No visible location sits off the prefab.
- Every visible location is reachable from the current node over rendered,
  unlocked paths.

**AZ1 simulation.** Every client map node is visited on the same snapshot,
following the client's rules:

1. `visit_node` for an unvisited node, then `StartLoc` for an enabled,
   unfinished location.
2. The follow-up the client sends: `conv_done`, `start` plus a won battle,
   `empty_done`, or a Shroom Haus choice.
3. A revisit from a neighbour, starting with a `getcampstate` reconnect.

A gated node is forced open: it is made visible and unblocked, and the visit
starts from a completed neighbour. If hydration closes the gate again, the
visit is retried with each story opener: the Brink Ridge blockade result,
or each quest granted.

SceneData locations that are not client map nodes (`Node019B`…, `Node023A/B`,
`Node026A/B`) are listed as skipped.

**Dungeons.** Every DungeonTemplate is planned:

- A runnable dungeon is entered and played to the end with every battle won.
- An AZ2 dungeon has no overworld entrance yet. It is still played, but its
  findings are reported as TODO.
- Castle Crayburn's fresh state is checked on its own code path.

**AZ2** has no AREA runtime yet, so only its data is checked (paths, node
conversations, dungeon entrances).

The campaign suite runs the check on a fresh database:
`test_every_az1_node_keeps_the_client_contract` and
`test_every_dungeon_map_keeps_the_client_contract`. Known findings are listed
with their reason in `KNOWN_AZ1_MAP_PROBLEMS`. The comparison is exact, so a
fix has to remove its entry.

**Live save probe.** The probe prints a per-node report (OK, PROBLEM, KNOWN,
TODO, SKIPPED) for every campaign of every champion, or of the champions
named with `--champion`. It exits with 1 when it finds a problem.

The simulation writes campaign rows, so the probe always works on a
temporary copy of `--db` and never changes that file. Run the schema repair
(bootstrap) on a copy first, so the copy matches the code:

```bash
D="/path/to/HEX SHARDS OF FATE/Hex.app/Contents/Resources/Data"
docker run --rm --user 1001:1001 -v "$D:/client-data:ro" \
  -v /path/to/server-state:/hex/state:ro \
  -e HEX_GAMEDATA=/client-data/gamedata -e HEX_DB_PATH=/tmp/copy.db \
  -e HEX_RUN_TESTS_ON_BOOT=0 --entrypoint sh hex-server:latest -c '
    cd /hex && mkdir /tmp/live && cp /hex/state/hconnect.db* /tmp/live/ &&
    python3 -c "import sqlite3; s=sqlite3.connect(\"/tmp/live/hconnect.db\");
d=sqlite3.connect(\"/tmp/copy.db\"); s.backup(d)" &&
    python3 docker/docker_bootstrap.py >/dev/null 2>&1 &&
    python3 campaign_map_check.py --db /tmp/copy.db --champion 5'
```

The probe has two options:

- `--no-simulate` checks only the current states.
- `--no-dungeons` skips the dungeon runs.

A full report for one champion takes about a minute.

## QUEST

`QuestTemplate.jsonl` contains 33 physical lines, but the first is a section
delimiter: there are 32 actual quest-template records in the current extract.
Quest templates contain the quest ID, script name, title/description, and an
ordered list of objectives. They do **not** contain gold, XP, card, equipment,
or loot fields.

The client source defines these objective types:

| Objective type | Metadata reference |
|---|---|
| `QuestObjectiveEncounter` | `m_EncounterId` |
| `QuestObjectiveDungeon` | `m_DungeonId` |
| `QuestObjectiveConversation` | conversation IDs and optional `m_ConversationEvent` |
| `QuestObjectiveCollect` | count and public script variable |

Every objective also has a `m_QuestLocationId`. This is a quest-state location
identifier, not necessarily an overworld map node. The server stores the active
objectives as `QUEST` campaign `VisLocs` with matching `Location.Name` values.

The current implementation reveals only the first objective initially. The
complete ordered objective list is retained in `GameplayState.Flags`, and
`_advance_quest_campaign` marks the current `VisLoc` complete and appends the
next one. This prevents future journal objectives from appearing before they
are reachable.

Quest grant and completion are runtime events rather than fields on
`QuestTemplate`:

- grant: a campaign script/event creates a `QUEST` row and sends `campspawn`;
- encounter objective: matched scene wins advance the objective;
- dungeon objective: dungeon completion advances the objective;
- conversation objective: `conv_done` completes the active objective;
- collection objective: progress is based on the relevant persisted variable.

Quest-start map changes are metadata-driven. `quest_templates.start_hook` stores
the name of a whitelisted Python hook in `campaign.py`; when the conversation
catalog grants that quest, the hook is called and is replayed for active quests
when the area state is loaded. The current AZ1 gates are seeded as:

| Quest script | `start_hook` | Effect |
|---|---|---|
| `az01_tamed` | `az1_tamed_start` | hide/block Node005 (Fonferek Thicket) |
| `q_seawitch` | `az1_find_horwich_sea_start` | unblock/reveal Node005 |
| `az01_uw_find_cave_in` | `az1_find_cave_in_start` | reveal/mark Node017 and hide Node034 |
| `az01_ar_find_ambling_mesa` | `az1_find_ambling_mesa_start` | reveal/mark Node034 and hide Node017 |

Adding another quest-specific map transition therefore requires a named hook
and a `start_hook` value on the quest row; the quest grant path itself does not
branch on the quest script or node number.

The extracted `m_ConversationEvent` values are empty in the current data, so the
server currently uses explicit campaign events and GUID mappings.

## Linkage between conversations, scenes, encounters, and quests

The practical relationship is:

```text
ConversationTemplate GUID
        │
        └── Location.ConversationID
                │  client closes conversation
                └── sendevent conv_done

SceneData node ──► Location.Node/Name
                      │
                      └── Location.Encounter (scene GUID)
                              │
                              └── encounter_scenes
                                  ├── AI deck/champion
                                  ├── mods_json
                                  └── rewards_json

QuestTemplate script
        └── QuestObjective*.m_QuestLocationId
                │
                └── QUEST GameplayState.VisLocs
```

Starting an encounter persists `ActiveEncounterGuid` on the campaign state.
The battle setup uses that GUID to resolve the opponent and deck; the game-end
reward path uses the same GUID to resolve `rewards_json`. This avoids falling
back to the race trainer scene when an AZ1/AZ2 node is being played.

## Encounter rewards

Rewards are authored on `encounter_scenes.rewards_json`, not on quest templates.
The schema is intentionally JSON so a scene can have one compact record or a
list under `end_of_game_rewards`/`end_of_game`:

```json
{"gold": 100, "xp": 100}
```

```json
{
  "end_of_game_condition": {"type": "void_tamed_troop", "owner": "opponent"},
  "card_guid": "$condition.template_guid",
  "quantity": 1,
  "one_time": true,
  "gold": 100,
  "xp": 100
}
```

Supported authored fields in the current server are:

| Field | Meaning |
|---|---|
| `gold` | Add to `users.gold` and emit a `GOLD/GRANT` reward |
| `xp` | Add to `champions.xp`, recalculate level, and emit `XP/GRANT` |
| `card_guid` or `template` | Add card copies to the collection |
| `quantity` | Number of card copies; defaults to one |
| `one_time` | Claim once per campaign scene; defaults to true |
| `end_of_game_condition` | Gate a conditional reward |
| `end_of_game_rewards`/`end_of_game` | List of reward records |
| `card_choice` | Card choices for a Shroom Haus location |

The current condition evaluator supports `void_tamed_troop`. It looks in the
finished game session's Void for an opponent-owned troop with the persisted
`permanent_buffs.int_attrs.Tamed` marker. `$condition.template_guid` resolves
to that captured card's template GUID. A failed conditional taming encounter
remains retryable; the node is not treated as permanently complete.

`card_choice` is handled separately from battle end. The client sends
`sendevent` with `Event=shroom_choice`; the server validates the selected GUID
against the scene's authored choices, adds the card to the collection, emits
the card reward, and completes the Shroom Haus location. The server does not
choose one of the three cards automatically.

### Reward delivery to the client

For a campaign battle win, `handle_battle_gameend` applies the scene rewards
before sending `gameendnotify`. The payload uses the client-compatible
`AppliedUpdates` shape:

- `Accounts` contains the updated balance;
- `Champions` contains updated XP/level;
- `Cards` contains newly granted card instances;
- `Completed` contains `ItemAction=GRANT` entries that trigger the client's
  pending campaign loot window.

The client recognizes additional `Completed.ItemKind` values such as `LEVEL`,
`CARD`, `BOACARD`, `BOAITEM`, `ITEM`, `MERCPAC`, and `MERCXP`. The current
`rewards_json` evaluator directly authors only gold, XP, card GUIDs, and Shroom
Haus card choices; other reward kinds require corresponding server-side
`AppliedUpdates` handling.

Rewards are not emitted for a loss. One-time claims are persisted in the
campaign state's `_encounter_reward_claims`, so reconnecting or retrying a
completed scene does not duplicate them.

## Champion XP thresholds

The current campaign reward path treats XP as cumulative champion XP. The
server recalculates the level using these thresholds:

| Level | Total XP required |
|---:|---:|
| 1 | 0 |
| 2 | 1,000 |
| 3 | 2,800 |
| 4 | 5,000 |
| 5 | 8,000 |
| 6 | 12,000 |
| 7 | 17,500 |
| 8 | 25,000 |
| 9 | 40,000 |
| 10 | 50,000 |
| 11 | 60,000 |
| 12 | 70,000 |
| 13 | 80,000 |
| 14 | 92,000 |
| 15 | 110,000 |

The current implementation caps this calculation at level 15. A 100 XP
encounter reward therefore updates the champion's cumulative XP but normally
does not level a new champion immediately.

## Scene and reward data maintenance

`encounter_scenes` is created in `static.py`. Existing databases receive
`ai_champion_guid`, `mods_json`, and `rewards_json` through the schema-repair
path in the same module. Encounter and deck rows are seeded from the extracted
gamedata; authored AZ0/tutorial/AZ1 reward defaults are then merged without
replacing scene-specific card choices or conditional rewards.

Useful inspection queries:

```sql
SELECT guid, name, ai_deck_guid, ai_champion_guid, mods_json, rewards_json
FROM encounter_scenes
WHERE name LIKE 'AZ 1%';

SELECT id, champion_id, template_name, campaign_type, state_json
FROM campaigns
WHERE champion_id = ?
ORDER BY id;
```

When changing campaign code, validate with:

```bash
python3 -m py_compile campaign.py
python3 tests/verify_goldens.py
```

For client-visible failures, inspect
`/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt`
first, then `/tmp/hconnect_log.txt` and `/tmp/hconnect_requests.log`. Changes
to `campaign.py` require `bash restart.sh`; reload-only modules can use the
server's `SIGUSR1` hot-reload path.
