"""Map-wide campaign check: does every campaign node work? (issue #31, C)

The client renders a campaign location only when the server keeps the
contract below; a broken location hangs the map (issues #23 and #31: a
player-less scene sent as an ``Encounter`` threw in
``EncounterScene.GetChampionTemplate``).  This module checks that contract
for whole maps, driving ``campaign.py`` through its real request handlers:

* **State contract** (every location of a state):

  - ``node`` is a ``LocNodes`` entry and ``type`` is a client
    ``LocationType`` name;
  - ``Encounter``: the scene is in ``Encounters`` (``GetEncounterID``) and in
    ``encounter_scenes``, with an AI deck and an AI champion;
  - ``Convo``: ``conversationId`` is a known ConversationTemplate;
  - ``Panorama``: the scene exists;
  - ``Dungeon``: a runnable DungeonTemplate (``campaign._dungeon_plan``); the
    only accepted fallback is a safe ``Empty`` with ``dungeon_unavailable``;
  - dungeon terrain (``TerrainType: Dungeon``, a ``- DUNGEON`` scene) is
    never an ``Encounter``;
  - an encounter reference the node info panel resolves (in ``Encounters``
    and repeatable) needs an AI deck as well;
  - every map path endpoint exists, and every visible location is reachable
    from the current node over rendered, unlocked paths.

* **Simulated visits** (AREA maps): for every node, on the same snapshot,
  ``visit_node`` + ``StartLoc``, then the client's follow-up (``conv_done``,
  ``start`` + a won battle, ``empty_done``, a Shroom Haus choice), then a
  revisit from a neighbour.  Each response must succeed and keep the
  contract.  A gated node is forced open (visible, unblocked, a completed
  neighbour to arrive from); nodes are never skipped silently.

* **Dungeons**: every DungeonTemplate is planned; a runnable one is entered
  and played to the end (every battle won), checking the contract at every
  step.

``check_database`` runs everything for every champion in a database; the
command line prints a per-node report.  The simulation writes campaign rows,
so the command line always works on a temporary copy of the database and
never changes the file it is given::

    python3 campaign_map_check.py --db /path/to/hconnect.db [--champion 5]

The module is imported lazily by the CLI so ``HEX_DB_PATH`` can point the
runtime at the copy before ``campaign``/``db`` open a connection.  All state
is local to a call (one recording handler per check), so concurrent checks on
separate connections do not interfere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import zlib
from pathlib import Path

ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Game.Shared.Eval.Data.LocationType (Location.GetLocationType uses a
# case-sensitive Enum.Parse; "None" falls back on the encounter/conversation).
CLIENT_LOCATION_TYPES = frozenset({
    "Empty", "Convo", "Encounter", "Dungeon", "Panorama", "ShroomHaus",
    "ModChoiceEncounter", "CountPicker", "ModDisplay", "ChampionOverride",
})

# Overworld nodes that legitimately cannot be played on this server.  The
# check still visits them and requires the safe fallback; it only accepts the
# listed reason instead of reporting it.
KNOWN_UNAVAILABLE = {
    ("AZ1", "Node011"): "Fort Romor: the DungeonTemplate rooms are on nodes "
                        "the client prefab campaign/az01/fortromor lacks",
}

# Maps the check covers.  AZ2 has seeded paths and scenes but no AREA
# runtime yet (campaign.py builds only the Howling Plains map), so only its
# data is checked.
AREA_MAPS = {
    "AZ1": {"scene_data": "Howling Plains", "runtime": True},
    "AZ2": {"scene_data": "The Alachian Sea", "runtime": False,
            "reason": "no AZ2 AREA runtime in campaign.py (only the AZ1 "
                      "Howling Plains map is built); data checked only"},
}

_cache_lock = threading.Lock()
_conversation_cache: dict[str, str] = {}
_scene_data_cache: dict[str, list] = {}


# ── static data ─────────────────────────────────────────────────────────

def conversation_names() -> dict[str, str]:
    """Every ConversationTemplate the client knows (GUID -> name)."""
    with _cache_lock:
        if _conversation_cache:
            return _conversation_cache
        from AssetExtraction import gamedata_seed
        records = gamedata_seed.configured_records_path()
        path = Path(records) / "ConversationTemplate.jsonl"
        names: dict[str, str] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, str):
                    continue
                guid = re.search(
                    r'"m_Id"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', raw)
                name = re.search(r'"m_Name"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                if guid and name:
                    names[guid.group(1).lower()] = name.group(1)
        if not names:
            if gamedata_seed.configured_path():
                data = gamedata_seed.load_text()
            else:
                data = gamedata_seed.load_records_text(records)
            names = gamedata_seed._conversation_names(data)
        _conversation_cache.update(names)
        return _conversation_cache


def scene_data_items(name: str) -> list:
    """``m_ItemData`` of one SceneData record (a map's authored nodes)."""
    with _cache_lock:
        if name in _scene_data_cache:
            return _scene_data_cache[name]
        import yaml
        from AssetExtraction import gamedata_seed
        path = Path(gamedata_seed.configured_records_path()) / "SceneData.jsonl"
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, str) or f'"m_Name" : "{name}"' not in raw:
                continue
            record = yaml.safe_load(raw)
            if isinstance(record, dict) and record.get("m_Name") == name:
                items = record.get("m_ItemData") or []
                break
        _scene_data_cache[name] = items
        return items


def _scene_battle_ready(db, scene_guid):
    """``(ok, reason)``: the scene can be played and shown as an opponent
    (``EncounterScene.GetChampionTemplate`` finds an AI deck and champion)."""
    import pve_db
    row = pve_db.db_encounter_scene_runtime(scene_guid, db) if scene_guid else None
    if not row:
        return False, f"scene {scene_guid} is not in encounter_scenes"
    deck, champion = row[4], row[5]
    if not deck or str(deck) == ZERO_GUID:
        return False, f"scene {row[1]!r} has no AI deck"
    if not champion or str(champion) == ZERO_GUID:
        return False, f"scene {row[1]!r} has no AI champion"
    if not db.execute("SELECT 1 FROM encounter_deck_cards WHERE deck_guid=? "
                      "LIMIT 1", (deck,)).fetchone():
        return False, f"scene {row[1]!r}: AI deck {deck} has no cards"
    return True, None


# ── protocol driver ─────────────────────────────────────────────────────

def _envelope(dw_bytes):
    """The JSON campaign envelope of a sent (gzip) DataWrapper, or None for
    a non-campaign message (collection or profile updates)."""
    try:
        start = dw_bytes.index(bytes([0x1F, 0x8B, 0x08]))
        inner = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(
            dw_bytes[start:])
        begin = inner.index(b'{"')
        return json.JSONDecoder().raw_decode(
            inner[begin:].decode("utf-8", "replace"))[0]
    except (ValueError, zlib.error):
        return None


class RecordingHandler:
    """A connection stand-in that records what the server sends."""

    client_uid = 1
    client_reck_id = "0"
    sid = "campaign-map-check"

    def __init__(self):
        self.scnt = 0
        self.sent = []

    def send(self, _headers, body=b""):
        self.sent.append(body)

    def _log_req(self, *_args):
        return None


class Driver:
    """Sends client requests through ``campaign.handle_campaign_request``."""

    def __init__(self, db):
        import campaign
        self.campaign = campaign
        self.db = db
        self.handler = RecordingHandler()

    def request(self, payload):
        """Return ``(response, pushes, error)`` for one client request."""
        start = len(self.handler.sent)
        try:
            self.campaign.handle_campaign_request(
                self.handler, self.db,
                {"Envelope": json.dumps(payload).encode()}, 1, ZERO_GUID, 2,
                "ServiceCampaign", "253", 0, 1)
        except Exception as exc:  # reported as a node problem
            return None, [], f"{payload.get('RequestType')} raised {exc!r}"
        messages = [_envelope(body) for body in self.handler.sent[start:]]
        response = next((m for m in messages if isinstance(m, dict)
                         and "Cid" in m and "RequestType" not in m), None)
        pushes = [m for m in messages if isinstance(m, dict)
                  and m.get("RequestType")]
        return response, pushes, None

    def visit_node(self, camp_id, node):
        return self.request({"RequestType": "sendevent", "CampID": camp_id,
                             "Event": "visit_node", "OParms": [node]})

    def startloc(self, camp_id, name):
        return self.request({"RequestType": "locaction", "CampID": camp_id,
                             "RAct": 0, "Loc": name, "Params": []})

    def event(self, camp_id, name, params=None):
        return self.request({"RequestType": "sendevent", "CampID": camp_id,
                             "Event": name, "OParms": params})

    def state_request(self, camp_id):
        return self.request({"RequestType": "getcampstate", "CampID": camp_id})

    def startcamp(self, camp_id):
        return self.request({"RequestType": "startcamp", "CampID": camp_id})

    def battle_won(self, camp_id):
        """The battle launched by ``start`` ends in a win."""
        campaign = self.campaign
        start = len(self.handler.sent)
        try:
            campaign._apply_gameend(self.db, camp_id, True)
            campaign.advance_crayburn_step(
                self.handler, self.db, camp_id, True, 1, ZERO_GUID,
                "ServiceCampaign", "253", 0, 1, auto_activate=False)
        except Exception as exc:
            return [], f"battle end raised {exc!r}"
        return [m for m in map(_envelope, self.handler.sent[start:])
                if m is not None], None


# ── state contract ──────────────────────────────────────────────────────

def _locations(state):
    return [loc.get("Data") or {} for loc in (state or {}).get("VisLocs") or []]


def _location(state, node):
    return next((data for data in _locations(state)
                 if node in (data.get("node"), data.get("name"))), None)


def location_problems(db, state, data, *, template=None, terrain=None):
    """Contract problems of one location (a list of strings)."""
    import campaign
    problems = []
    nodes = {str(item.get("Name")) for item in state.get("LocNodes") or []}
    catalog = {item.get("Name"): (item.get("Data") or {}).get("encscene")
               for item in state.get("Encounters") or []}
    node = data.get("node")
    kind = data.get("type")
    if node not in nodes:
        problems.append(f"node {node!r} is not in LocNodes")
    if kind not in CLIENT_LOCATION_TYPES:
        problems.append(f"type {kind!r} is not a client LocationType")
    encounter = data.get("encounter")
    if kind == "Encounter":
        if not encounter:
            problems.append("Encounter without a scene")
        elif not catalog.get(encounter):
            problems.append(f"Encounter scene {encounter} is not in "
                            "GameplayState.Encounters")
        else:
            ok, reason = _scene_battle_ready(db, catalog[encounter])
            if not ok:
                problems.append(f"Encounter: {reason}")
    elif kind == "Convo":
        conversation = str(data.get("conversationId") or "").lower()
        if not conversation:
            problems.append("Convo without a conversationId")
        elif conversation not in conversation_names():
            problems.append(f"Convo conversation {conversation} is not a "
                            "ConversationTemplate")
    elif kind == "Panorama":
        import pve_db
        if not encounter or not pve_db.db_encounter_scene_exists(encounter, db):
            problems.append(f"Panorama scene {encounter!r} does not exist")
    elif kind == "Dungeon" and state.get("TempType") == "AREA":
        # An overworld portal (Crayburn's own DUNGEON map uses the type for
        # its entry marker and has no DungeonTemplate behind it).
        plan, reason = campaign._dungeon_plan(
            db, state.get("ChampID"), campaign._dungeon_template(
                db, guid=data.get("dungeon_template")))
        if not plan:
            problems.append(f"Dungeon portal is not runnable: {reason}")
    elif kind == "ShroomHaus" and not data.get("completed"):
        if not data.get("choices"):
            problems.append("ShroomHaus without card choices")
    if (kind != "Encounter" and encounter and catalog.get(encounter) and
            data.get("repeatable")):
        # UICampaignNodeInfoPanel resolves a repeatable location's encounter
        # and calls EncounterScene.GetChampionTemplate on it.
        ok, reason = _scene_battle_ready(db, catalog[encounter])
        if not ok:
            problems.append(f"node panel opponent lookup: {reason}")
    dungeon_terrain = terrain == "Dungeon"
    if encounter:
        import pve_db
        row = pve_db.db_encounter_scene_runtime(encounter, db)
        dungeon_terrain = dungeon_terrain or bool(
            row and str(row[1] or "").upper().rstrip().endswith("- DUNGEON"))
    if dungeon_terrain and kind == "Encounter":
        problems.append("dungeon terrain sent as an Encounter")
    if dungeon_terrain and kind not in ("Dungeon",):
        allowed = KNOWN_UNAVAILABLE.get((template, node))
        if not (kind == "Empty" and data.get("dungeon_unavailable") and allowed):
            if not (kind == "Empty" and data.get("dungeon_unavailable")):
                problems.append(f"dungeon entrance sent as {kind!r}")
            else:
                problems.append("dungeon entrance unavailable: "
                                f"{data.get('dungeon_unavailable')}")
    return problems


def _edges(db, template):
    import pve_db
    return [(str(a), str(b), str(p or "")) for a, b, p in
            pve_db.db_template_edges(template, db)]


def map_problems(db, state, template, *, edges=None):
    """Path/visibility problems of a map state: ``{node: [problems]}``."""
    import campaign
    problems: dict[str, list] = {}
    edges = _edges(db, template) if edges is None else edges
    nodes = {str(item.get("Name")) for item in state.get("LocNodes") or []}
    data_by_node = {d.get("node"): d for d in _locations(state)}
    prefab_nodes = {node for start, end, _path in edges for node in (start, end)}
    for node in sorted(prefab_nodes):
        if node not in nodes or node not in data_by_node:
            problems.setdefault(node, []).append(
                "client map node (path endpoint) has no location/LocNode, "
                "so the client never shows it")
    for node, data in data_by_node.items():
        if (prefab_nodes and node not in prefab_nodes and
                data.get("visible") and data.get("enabled")):
            problems.setdefault(node, []).append(
                "visible location is not a node of the client map prefab")
    pdata = (state.get("PublicState") or {}).get("Data") or {}
    locked = {str(path) for path in pdata.get("locked_paths") or []}
    adjacency: dict[str, set] = {}
    for start, end, path in edges:
        name = campaign._az1_path_name(start, end, path)
        if name in locked:
            continue
        if not all((data_by_node.get(n) or {}).get("visible") for n in
                   (start, end)):
            continue
        adjacency.setdefault(start, set()).add(end)
        adjacency.setdefault(end, set()).add(start)
    origin = campaign._resolve_node(
        state, state.get("LastNode") or state.get("ALoc") or "")
    if not origin or origin not in data_by_node:
        origin = next(iter(data_by_node), "")
    seen, frontier = {origin}, [origin]
    while frontier:
        for nxt in adjacency.get(frontier.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    # A quest destination is revealed ahead of its route (Ambling Mesa,
    # Shadowgrove); the client shows it without a Travel button until a path
    # opens, so only an ordinary location must be reachable.
    quest_marked = {str(node) for key in ("quest_nodes", "unlocked_nodes")
                    for node in pdata.get(key) or []}
    for node, data in data_by_node.items():
        if (data.get("visible") and data.get("enabled") and
                node not in seen and node not in quest_marked and
                not str(node).lower().startswith("fork")):
            problems.setdefault(node, []).append(
                f"visible but unreachable from {origin} over unlocked paths")
    return problems


def state_problems(db, state, template=None, terrain=None):
    """Every contract problem of a state, keyed by node."""
    terrain = terrain or {}
    problems: dict[str, list] = {}
    for data in _locations(state):
        found = location_problems(db, state, data, template=template,
                                  terrain=terrain.get(data.get("node")))
        if found:
            problems.setdefault(str(data.get("node")), []).extend(found)
    return problems


def quest_problems(db, state):
    """A QUEST state is a journal, not a map: its objectives must reference
    real conversations and battle-ready scenes."""
    problems: dict[str, list] = {}
    catalog = {item.get("Name") for item in state.get("Encounters") or []}
    for data in _locations(state):
        node = str(data.get("node"))
        conversation = str(data.get("conversationId") or "").lower()
        if conversation and conversation not in conversation_names():
            problems.setdefault(node, []).append(
                f"objective conversation {conversation} is not a "
                "ConversationTemplate")
        encounter = data.get("encounter")
        if data.get("type") == "Encounter" and encounter:
            ok, reason = _scene_battle_ready(db, encounter)
            if not ok:
                problems.setdefault(node, []).append(f"objective: {reason}")
            elif encounter not in catalog:
                problems.setdefault(node, []).append(
                    f"objective scene {encounter} is not in Encounters")
    return problems


# ── snapshots ───────────────────────────────────────────────────────────

class ChampionSnapshot:
    """Campaign rows and reconnect pointer of one champion, restorable."""

    def __init__(self, db, champion_id):
        self.db = db
        self.champion_id = champion_id
        cursor = db.execute("SELECT * FROM campaigns WHERE champion_id=?",
                            (champion_id,))
        self.columns = [item[0] for item in cursor.description]
        self.rows = cursor.fetchall()
        row = db.execute("SELECT last_campaign_id FROM champions WHERE id=?",
                         (champion_id,)).fetchone()
        self.last_campaign = row[0] if row else None

    def restore(self):
        db = self.db
        db.execute("DELETE FROM campaigns WHERE champion_id=?",
                   (self.champion_id,))
        marks = ",".join("?" for _ in self.columns)
        db.executemany(
            f"INSERT INTO campaigns ({','.join(self.columns)}) VALUES ({marks})",
            self.rows)
        db.execute("UPDATE champions SET last_campaign_id=? WHERE id=?",
                   (self.last_campaign, self.champion_id))
        db.commit()


def _load_state(db, camp_id):
    row = db.execute("SELECT state_json FROM campaigns WHERE id=?",
                     (camp_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _save_state(db, camp_id, state):
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), camp_id))
    db.commit()


# ── AREA simulation ─────────────────────────────────────────────────────

class NodeReport:
    def __init__(self, campaign_label, node, title=""):
        self.campaign = campaign_label
        self.node = node
        self.title = title
        self.problems: list[str] = []
        self.notes: list[str] = []

    @property
    def ok(self):
        return not self.problems

    def add(self, stage, problems):
        for problem in problems or []:
            text = f"{stage}: {problem}"
            if text not in self.problems:
                self.problems.append(text)

    def line(self):
        status = "OK" if self.ok else "PROBLEM"
        label = f"{self.node} {self.title}".strip()
        text = f"  {status:<7} {label}"
        if self.notes:
            text += f"  [{'; '.join(self.notes)}]"
        for problem in self.problems:
            text += f"\n            - {problem}"
        return text


def _area_terrain(template):
    info = AREA_MAPS.get(template) or {}
    return {item.get("m_MapNodeId"): item.get("m_TerrainType")
            for item in scene_data_items(info.get("scene_data", ""))}


def _real_neighbours(db, node):
    """Non-fork locations one path (or one fork) away from *node*."""
    import campaign
    forks = campaign._az1_path_fork_ids(db)
    result = set()
    for neighbour in campaign._az1_neighbors(db, node):
        if neighbour in forks:
            result |= {n for n in campaign._az1_neighbors(db, neighbour)
                       if n not in forks and n != node}
        else:
            result.add(neighbour)
    return result


def _force_arrival(db, state, node, origin, report):
    """Open *node*'s gates and stand on *origin*, ready to walk there."""
    pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
    target = _location(state, node)
    gated = []
    if not target.get("visible") or not target.get("enabled"):
        gated.append("hidden")
    for key, label in (("blocked_nodes", "quest-blocked"),
                       ("quest_hidden_nodes", "quest-hidden")):
        kept = [n for n in pdata.get(key) or [] if n != node]
        if len(kept) != len(pdata.get(key) or []):
            gated.append(label)
            pdata[key] = kept
    target.update({"visible": True, "enabled": True})
    if origin:
        start = _location(state, origin)
        if start is not None:
            start.update({"visible": True, "enabled": True})
            if not start.get("completed") and not start.get("repeatable"):
                start["completed"] = True
            visited = pdata.setdefault("visited_nodes", [])
            if origin not in visited:
                visited.append(origin)
    state.update({"LastNode": origin or node, "ALoc": None,
                  "CurState": "EXPLORE"})
    if gated:
        report.notes.append("forced open (" + ", ".join(gated) + ")")


def _check_response(db, report, stage, result, template, terrain):
    response, pushes, error = result
    if error:
        report.add(stage, [error])
        return None, pushes
    if not response:
        report.add(stage, ["no response"])
        return None, pushes
    if not response.get("Success"):
        report.add(stage, [f"failed: {response.get('Errors')}"])
        return None, pushes
    state = response.get("CurState") or {}
    found = state_problems(db, state, template, terrain)
    report.add(stage, found.get(report.node))
    for push in pushes:
        if push.get("RequestType") == "cmpupdate" and push.get("State"):
            pushed = push["State"]
            for node, items in state_problems(db, pushed).items():
                report.add(f"{stage} ({push.get('Reason')} push)",
                           [f"{node}: {item}" for item in items])
    return state, pushes


def _transitioned(pushes):
    return next((push for push in pushes
                 if push.get("RequestType") == "cmpupdate" and
                 push.get("Transition")), None)


def _play_location(driver, camp_id, report, stage, state, template, terrain,
                   limit=6):
    """Resolve the active location the way the client does."""
    db = driver.db
    for _step in range(limit):
        name = state.get("ALoc")
        if not name:
            return state
        data = _location(state, name)
        if data is None:
            report.add(stage, [f"ALoc {name!r} names no location"])
            return state
        kind = data.get("type")
        if kind == "Convo":
            played = data.get("conversationId")
            state, _pushes = _check_response(
                db, report, f"{stage} conv_done",
                driver.event(camp_id, "conv_done"), template, terrain)
            if state is None:
                return None
            after = _location(state, name) or {}
            if (state.get("ALoc") == name and after.get("type") == "Convo" and
                    after.get("conversationId") == played):
                report.add(stage, [f"conversation {played} replays after "
                                   "conv_done (ALoc stays on it)"])
                return state
        elif kind == "Encounter":
            response, pushes, error = driver.event(camp_id, "start")
            if error:
                report.add(f"{stage} start", [error])
                return None
            if not any(p.get("RequestType") == "gamestarted" for p in pushes):
                report.add(f"{stage} start", ["no battle was launched"])
                return state
            _messages, error = driver.battle_won(camp_id)
            if error:
                report.add(f"{stage} battle", [error])
                return None
            state = _load_state(db, camp_id)
            report.add(f"{stage} won", state_problems(
                db, state, template, terrain).get(report.node))
        elif kind == "Empty":
            state, _pushes = _check_response(
                db, report, f"{stage} empty_done",
                driver.event(camp_id, "empty_done"), template, terrain)
            if state is None:
                return None
            if state.get("ALoc") == name:
                report.add(stage, ["empty_done keeps ALoc on the location"])
                return state
        elif kind == "ShroomHaus":
            choice = (data.get("choices") or [""])[0]
            state, _pushes = _check_response(
                db, report, f"{stage} shroom_choice",
                driver.event(camp_id, "shroom_choice", [choice]),
                template, terrain)
            if state is None:
                return None
            if state.get("ALoc") == name:
                report.add(stage, [f"Shroom Haus choice {choice!r} was not "
                                   "accepted (ALoc stays on it)"])
                return state
        else:
            return state
    report.add(stage, [f"location did not resolve in {limit} steps"])
    return state


def _visit(driver, camp_id, report, stage, node, template, terrain,
           reconnect=True):
    """Arrive at *node* the way ``UIDungeonZoneViewModel`` does.

    Optionally a reconnect (``getcampstate``) first, then ``visit_node``
    when the node is not visited yet, then ``StartLoc`` only for an enabled,
    unfinished location (``AttemptStartCurrentMapNode``).  Returns
    ``(state, pushes, started)``.
    """
    db = driver.db
    if reconnect:
        state, _pushes = _check_response(
            db, report, f"{stage} getcampstate",
            driver.state_request(camp_id), template, terrain)
        if state is None:
            return None, [], False
    else:
        state = _load_state(db, camp_id)
    visited = ((state.get("PublicState") or {}).get("Data") or {}).get(
        "visited_nodes") or []
    if node not in visited:
        state, pushes = _check_response(
            db, report, f"{stage} visit_node",
            driver.visit_node(camp_id, node), template, terrain)
        if state is None:
            return None, [], False
        if _transitioned(pushes):
            return state, pushes, False
    data = _location(state, node) or {}
    if not data.get("enabled") or data.get("completed"):
        return state, [], False
    state, pushes = _check_response(
        db, report, f"{stage} StartLoc",
        driver.startloc(camp_id, data.get("name") or node), template, terrain)
    return state, pushes, state is not None


def gate_openers(db):
    """``[(name, opener)]`` that open story gates the way the game does.

    Each opener changes a fresh snapshot: the successful Brink Ridge
    blockade result, or granting one quest (its map transition, a
    ``start_hook`` or the Vale of Oberon faction branch, is replayed on
    hydration).  Quests with a ``start_hook`` are tried first.
    """
    import campaign

    def grant(script):
        def opener(db, champion_id, _camp_id):
            campaign._ensure_quest_campaign(db, champion_id, "AREA", script)
        return opener

    def clear_blockade(db, _champion_id, camp_id):
        state = _load_state(db, camp_id)
        state.setdefault("PublicState", {}).setdefault("Data", {})[
            "blockade_cleared"] = True
        _save_state(db, camp_id, state)

    rows = db.execute(
        "SELECT script_name FROM quest_templates "
        "ORDER BY COALESCE(start_hook, '')='', script_name").fetchall()
    return ([("Brink Ridge blockade won", clear_blockade)] +
            [(f"quest {script}", grant(script)) for (script,) in rows])


def client_map_nodes(db, template="AZ1"):
    """Node IDs of the client's map prefab (every path endpoint)."""
    return sorted({node for start, end, _path in _edges(db, template)
                   for node in (start, end)})


def simulate_area(db, camp_id, template="AZ1", nodes=None):
    """Visit every client map node of an AREA campaign.

    Returns ``(reports, skipped)``: one ``NodeReport`` per node, and
    ``{node: reason}`` for state locations that are not client map nodes
    (the client cannot select them; ``map_problems`` reports a visible one).
    """
    import campaign
    row = db.execute("SELECT champion_id FROM campaigns WHERE id=?",
                     (camp_id,)).fetchone()
    champion_id = row[0]
    terrain = _area_terrain(template)
    driver = Driver(db)
    snapshot = ChampionSnapshot(db, champion_id)
    base = _load_state(db, camp_id)
    forks = campaign._az1_path_fork_ids(db)
    prefab = set(client_map_nodes(db, template))
    skipped = {d.get("node"): "not a node of the client map prefab"
               for d in _locations(base)
               if d.get("node") and d.get("node") not in prefab}
    targets = nodes or [d.get("node") for d in _locations(base)
                        if d.get("node") in prefab and d.get("node") not in forks]
    reports = []
    label = f"{template} #{camp_id}"
    openers = [(None, None)] + gate_openers(db)
    try:
        for node in targets:
            # Fresh visit; a node the forced arrival cannot open (a quest or
            # story gate re-applied by hydration) is retried with each gate
            # opener in turn.
            for opener_name, opener in openers:
                snapshot.restore()
                if opener:
                    opener(db, champion_id, camp_id)
                state = _load_state(db, camp_id)
                data = _location(state, node) or {}
                report = NodeReport(label, node, data.get("title") or "")
                neighbours = sorted(_real_neighbours(db, node) & prefab)
                origin = next((n for n in neighbours
                               if (_location(state, n) or {}).get("visible")),
                              neighbours[0] if neighbours else None)
                _force_arrival(db, state, node, origin, report)
                if opener_name:
                    report.notes.append(f"opened by {opener_name}")
                _save_state(db, camp_id, state)
                state, pushes, started = _visit(
                    driver, camp_id, report, "visit", node, template, terrain,
                    reconnect=False)
                arrived = _location(state, node) or {} if state else {}
                if state is None or started or _transitioned(pushes) or \
                        arrived.get("completed") or arrived.get("enabled"):
                    break
            reports.append(report)
            if state is None:
                continue
            transition = _transitioned(pushes)
            if transition:
                report.notes.append(f"{transition.get('Reason')} -> "
                                    f"{transition.get('TemplateType')}")
            entered = started and state.get("ALoc") == arrived.get("name")
            if not started and not transition:
                if arrived.get("completed"):
                    report.notes.append("already completed")
                else:
                    report.add("visit", ["location is not enabled on arrival"])
                    continue
            elif started and not entered and not transition:
                report.add("visit", [
                    f"StartLoc did not enter the location (ALoc="
                    f"{state.get('ALoc')!r}, LastNode={state.get('LastNode')!r})"])
                continue
            if entered:
                state = _play_location(driver, camp_id, report, "visit",
                                       state, template, terrain)
                if state is None:
                    continue
            # Revisit from the neighbour once the location is resolved.
            state = _load_state(db, camp_id)
            if origin:
                state.update({"LastNode": origin, "ALoc": None,
                              "CurState": "EXPLORE"})
                _save_state(db, camp_id, state)
            state, pushes, started = _visit(driver, camp_id, report,
                                            "revisit", node, template, terrain)
            if state is None or not started:
                continue
            arrived = _location(state, node) or {}
            if state.get("ALoc") == arrived.get("name"):
                _play_location(driver, camp_id, report, "revisit", state,
                               template, terrain)
            elif not _transitioned(pushes):
                report.add("revisit", [
                    "StartLoc did not enter the unfinished location "
                    f"(ALoc={state.get('ALoc')!r})"])
    finally:
        snapshot.restore()
    return reports, skipped


def check_area_state(db, camp_id, template="AZ1"):
    """The contract of an AREA campaign as ``getcampstate`` returns it."""
    driver = Driver(db)
    response, _pushes, error = driver.state_request(camp_id)
    if error or not response:
        return {"*": [error or "getcampstate sent no response"]}, None
    state = response.get("CurState") or {}
    terrain = _area_terrain(template)
    problems = state_problems(db, state, template, terrain)
    for node, items in map_problems(db, state, template).items():
        problems.setdefault(node, []).extend(items)
    return problems, state


# ── dungeons ────────────────────────────────────────────────────────────

def dungeon_plans(db, champion_id):
    """``[(template, plan, reason)]`` for every seeded DungeonTemplate."""
    import campaign
    return [(template, *campaign._dungeon_plan(db, champion_id, template))
            for template in campaign._dungeon_template_rows(db)]


def dungeon_map_problems(db, champion_id, plan):
    """Contract of a fresh run plus its map: ``{node: [problems]}``."""
    import campaign
    state = campaign._build_generic_dungeon_state(0, champion_id, plan)
    problems = state_problems(db, state)
    # Prefab nodes without a room are never shown; a room is reachable only
    # through rooms and forks the client renders.
    nodes = {item["Name"] for item in state["LocNodes"]}
    edges = [(a, b, "") for a, b in plan["edges"] if a in nodes and b in nodes]
    adjacency: dict[str, set] = {}
    for start, end, _path in edges:
        adjacency.setdefault(start, set()).add(end)
        adjacency.setdefault(end, set()).add(start)
    seen, frontier = {plan["start"]}, [plan["start"]]
    while frontier:
        for nxt in adjacency.get(frontier.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    for room in plan["rooms"]:
        if room["stages"] and room["node"] not in seen:
            problems.setdefault(room["node"], []).append(
                f"room unreachable from {plan['start']}")
    return problems


def walk_dungeon(db, camp_id, limit=300):
    """Play a DungeonTemplate run to its end, winning every battle.

    Returns ``(state, {node: [problems]}, played_conversations)``; the
    contract is checked after every response.
    """
    driver = Driver(db)
    problems: dict[str, list] = {}
    played = []

    seen = set()

    def collect(state, stage):
        for node, items in state_problems(db, state).items():
            for item in items:
                if (node, item) not in seen:
                    seen.add((node, item))
                    problems.setdefault(node, []).append(
                        f"{item} (first at {stage})")

    def step(result, stage):
        response, _pushes, error = result
        if error or not response or not response.get("Success"):
            problems.setdefault("*", []).append(
                f"{stage}: {error or 'no successful response'}")
            return None
        state = response.get("CurState") or {}
        collect(state, stage)
        return state

    state = step(driver.startcamp(camp_id), "startcamp")
    for _ in range(limit):
        if state is None:
            return _load_state(db, camp_id), problems, played
        if state.get("Finished"):
            return state, problems, played
        active = _location(state, state.get("ALoc")) if state.get("ALoc") else None
        if active is None:
            open_rooms = [d for d in _locations(state)
                          if d.get("visible") and d.get("enabled")
                          and not d.get("completed")]
            if not open_rooms:
                problems.setdefault("*", []).append(
                    "stuck: no open room and the run is not finished")
                return state, problems, played
            node = open_rooms[0]["node"]
            state = step(driver.visit_node(camp_id, node), f"visit_node {node}")
            if state is not None:
                state = step(driver.startloc(camp_id, open_rooms[0]["name"]),
                             f"StartLoc {node}")
            continue
        node = active.get("node")
        if active.get("type") == "Convo":
            played.append(active.get("conversationId"))
            state = step(driver.event(camp_id, "conv_done"), f"conv_done {node}")
        elif active.get("type") == "Encounter":
            _response, pushes, error = driver.event(camp_id, "start")
            if error or not any(p.get("RequestType") == "gamestarted"
                                for p in pushes):
                problems.setdefault(node, []).append(
                    f"start: {error or 'no battle was launched'}")
                return _load_state(db, camp_id), problems, played
            _messages, error = driver.battle_won(camp_id)
            if error:
                problems.setdefault(node, []).append(f"battle: {error}")
                return _load_state(db, camp_id), problems, played
            state = _load_state(db, camp_id)
            collect(state, f"won {node}")
        else:
            state = step(driver.event(camp_id, "empty_done"),
                         f"empty_done {node}")
    problems.setdefault("*", []).append(f"run did not finish in {limit} steps")
    return state, problems, played


# DungeonTemplates the server does not run from an overworld entrance.
KNOWN_UNAVAILABLE_DUNGEONS = {
    "Fort Romor": "the template's rooms are on nodes the client prefab "
                  "campaign/az01/fortromor lacks (entrance is a safe Empty)",
    "Castle Crayburn": "runs on its own Crayburn code path (checked as the "
                       "Crayburn DUNGEON state)",
    "Howling Plains": "AREA SceneData template, not a dungeon",
    "The Alachian Sea": "AREA SceneData template, not a dungeon",
}
_NO_ENTRANCE = "no overworld entrance scene"


def check_dungeons(db, champion_id, walk=True):
    """Plan, map-check and (optionally) play every DungeonTemplate.

    Returns ``[(name, status, {node: [problems]})]``.  ``status`` is
    ``"finished"`` (played to the end), ``"runnable"`` (not walked),
    ``"unavailable: <reason>"`` or ``"known: <reason>"``.  A template that
    only lacks an overworld entrance (the AZ2 dungeons: AZ2 has no AREA
    runtime yet) is still planned and played, so its map and rooms are
    checked before an entrance exists.
    """
    import campaign
    results = []
    snapshot = ChampionSnapshot(db, champion_id) if walk else None
    try:
        for template, plan, reason in dungeon_plans(db, champion_id):
            name = template["name"]
            note = ""
            if not plan and reason == _NO_ENTRANCE and \
                    name not in KNOWN_UNAVAILABLE_DUNGEONS:
                plan, reason = campaign._dungeon_plan(
                    db, champion_id, dict(template, entrance_scene="-"))
                note = " (no overworld entrance yet)"
            if not plan:
                if name in KNOWN_UNAVAILABLE_DUNGEONS:
                    results.append((name, "known: " +
                                    KNOWN_UNAVAILABLE_DUNGEONS[name], {}))
                elif template.get("rooms"):
                    results.append((name, f"unavailable: {reason}", {}))
                continue
            problems = dungeon_map_problems(db, champion_id, plan)
            status = "runnable"
            if walk:
                snapshot.restore()
                camp_id, _state = campaign._enter_generic_dungeon(
                    db, champion_id, plan, plan.get("entrance_node"))
                final, walked, _played = walk_dungeon(db, camp_id)
                for node, items in walked.items():
                    known = problems.setdefault(node, [])
                    known.extend(item for item in items
                                 if item.split(" (first at ")[0] not in known)
                if final.get("FinishReason") == "Complete":
                    status = "finished"
                else:
                    problems.setdefault("*", []).append("run did not complete")
            results.append((plan["name"], status + note, problems))
    finally:
        if snapshot:
            snapshot.restore()
    return results


def check_crayburn(db, champion_id):
    """Contract of a fresh Castle Crayburn run for the champion's race."""
    import campaign
    champion = campaign._get_champion(db, champion_id)
    cfg = campaign._az0_config(champion[2]) if champion else None
    race = campaign._race_for_cfg(cfg) if cfg else None
    state = campaign._build_initial_gameplay_state(
        0, champion_id, "DUNGEON", race)
    state = campaign._prepare_dungeon_state(state, race)
    return state_problems(db, state)


# ── AZ2 (data only) ─────────────────────────────────────────────────────

def static_area_problems(db, template):
    """Data checks for a map without a server runtime: ``{node: [...]}``."""
    import campaign
    import pve_db
    info = AREA_MAPS[template]
    items = scene_data_items(info["scene_data"])
    nodes = {item.get("m_MapNodeId") for item in items}
    problems: dict[str, list] = {}
    if not items:
        return {"*": [f"no SceneData record {info['scene_data']!r}"]}
    for start, end, _path in _edges(db, template):
        for node in (start, end):
            if node not in nodes and not node.lower().startswith("fork"):
                problems.setdefault(node, []).append(
                    "path endpoint is not a SceneData node")
    entrances = {t["entrance_scene"] for t in campaign._dungeon_template_rows(db)}
    known = conversation_names()
    number = template[-1]
    scenes = db.execute(
        "SELECT guid, name FROM encounter_scenes WHERE name LIKE ?",
        (f"AZ {number} - NODE %",)).fetchall()
    for item in items:
        node = item.get("m_MapNodeId")
        for guid, _trigger, _prio, name in pve_db.db_node_conversation_rows(
                node, template, db):
            if str(guid).lower() not in known:
                problems.setdefault(node, []).append(
                    f"node conversation {name!r} is not a ConversationTemplate")
        if item.get("m_TerrainType") == "Dungeon":
            key = re.sub(r"^Node0*", "", node or "")
            portal = [g for g, n in scenes
                      if re.search(rf"NODE\s*0*{re.escape(key)}\b", n or "", re.I)
                      and g in entrances]
            if not portal:
                problems.setdefault(node, []).append(
                    "dungeon terrain without a DungeonTemplate entrance scene")
    return problems


# ── whole database ──────────────────────────────────────────────────────

def check_database(db, champion_ids=None, simulate=True, walk=True, out=None):
    """Check every campaign of every (or the given) champion.

    Prints a per-node report to *out* and returns the number of problems.
    """
    out = out or sys.stdout
    total = 0
    if champion_ids:
        champions = [(cid,) for cid in champion_ids]
    else:
        champions = db.execute(
            "SELECT DISTINCT champion_id FROM campaigns ORDER BY 1").fetchall()
    for template, info in AREA_MAPS.items():
        if not info["runtime"]:
            # Not counted: nothing on this map can be reached yet.
            problems = static_area_problems(db, template)
            print(f"{template}: {info['reason']}", file=out)
            for node, items in sorted(problems.items()):
                for item in items:
                    print(f"  TODO    {node}: {item}", file=out)
            if not problems:
                print("  data OK", file=out)
    for (champion_id,) in champions:
        name = db.execute("SELECT champion_name FROM champions WHERE id=?",
                          (champion_id,)).fetchone()
        print(f"\nChampion {champion_id} {name[0] if name else ''}".rstrip(),
              file=out)
        rows = db.execute(
            "SELECT id, campaign_type, template_name, state_json FROM campaigns "
            "WHERE champion_id=? ORDER BY id", (champion_id,)).fetchall()
        for camp_id, ctype, template, raw in rows:
            ctype = str(ctype or "").upper()
            label = f"  campaign #{camp_id} {ctype} {template}"
            if ctype == "AREA" and str(template).upper() == "AZ1":
                problems, state = check_area_state(db, camp_id, "AZ1")
                print(f"{label}: current state", file=out)
                count = sum(len(v) for v in problems.values())
                total += count
                for node, items in sorted(problems.items()):
                    for item in items:
                        print(f"    PROBLEM {node}: {item}", file=out)
                if not count:
                    print(f"    OK ({len(_locations(state))} locations)",
                          file=out)
                if simulate:
                    reports, skipped = simulate_area(db, camp_id, "AZ1")
                    bad = [r for r in reports if not r.ok]
                    total += sum(len(r.problems) for r in bad)
                    print(f"{label}: simulated visits "
                          f"{len(reports) - len(bad)}/{len(reports)} OK",
                          file=out)
                    for report in reports:
                        print("  " + report.line(), file=out)
                    for node, reason in sorted(skipped.items()):
                        print(f"    SKIPPED {node}: {reason}", file=out)
            elif ctype == "DUNGEON":
                state = json.loads(raw) if raw else {}
                import campaign
                if campaign._is_generic_dungeon(state):
                    problems = state_problems(db, campaign._drun_refresh(state))
                else:
                    state = campaign._prepare_dungeon_state(
                        state, campaign._race_name_for_campaign(db, camp_id))
                    problems = state_problems(db, state)
                count = sum(len(v) for v in problems.values())
                total += count
                status = "finished" if state.get("Finished") else "open"
                print(f"{label} ({status}): "
                      f"{'OK' if not count else f'{count} problem(s)'}",
                      file=out)
                for node, items in sorted(problems.items()):
                    for item in items:
                        print(f"    PROBLEM {node}: {item}", file=out)
            else:
                state = json.loads(raw) if raw else {}
                problems = (quest_problems(db, state) if ctype == "QUEST"
                            else state_problems(db, state)
                            if state.get("LocNodes") else {})
                count = sum(len(v) for v in problems.values())
                total += count
                print(f"{label}: {'OK' if not count else f'{count} problem(s)'}",
                      file=out)
                for node, items in sorted(problems.items()):
                    for item in items:
                        print(f"    PROBLEM {node}: {item}", file=out)
        if walk:
            print("  dungeons (fresh run from the entrance, all battles won):",
                  file=out)
            crayburn = check_crayburn(db, champion_id)
            count = sum(len(v) for v in crayburn.values())
            total += count
            print(f"    {'OK' if not count else 'PROBLEM':<7} Castle Crayburn: "
                  "fresh state", file=out)
            for node, items in sorted(crayburn.items()):
                for item in items:
                    print(f"      - {node}: {item}", file=out)
            for name, status, problems in check_dungeons(db, champion_id):
                count = sum(len(v) for v in problems.values())
                if status.startswith("known"):
                    print(f"    KNOWN   {name}: {status[7:]}", file=out)
                    continue
                if status.startswith("unavailable"):
                    print(f"    PROBLEM {name}: {status}", file=out)
                    total += 1
                    continue
                # A dungeon without an overworld entrance cannot be reached
                # yet (AZ2): report, do not count.
                reachable = "no overworld entrance" not in status
                total += count if reachable else 0
                flag = "OK" if not count else (
                    "PROBLEM" if reachable else "TODO")
                print(f"    {flag:<7} {name}: {status}", file=out)
                for node, items in sorted(problems.items()):
                    for item in items:
                        print(f"      - {node}: {item}", file=out)
    print(f"\n{total} problem(s)", file=out)
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check that every campaign node works (issue #31). "
                    "Works on a temporary copy; the database is not changed.")
    parser.add_argument("--db", default=os.environ.get("HEX_DB_PATH"),
                        help="database to check (default: $HEX_DB_PATH)")
    parser.add_argument("--champion", type=int, action="append",
                        help="limit to a champion ID (repeatable)")
    parser.add_argument("--no-simulate", action="store_true",
                        help="only check the current states, no visits")
    parser.add_argument("--no-dungeons", action="store_true",
                        help="do not play the DungeonTemplate runs")
    args = parser.parse_args(argv)
    if not args.db or not os.path.isfile(args.db):
        parser.error("--db must name an existing database")
    handle, copy = tempfile.mkstemp(prefix="hex-map-check-", suffix=".db")
    os.close(handle)
    source = sqlite3.connect(f"file:{os.path.abspath(args.db)}?mode=ro",
                             uri=True)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    # campaign/db open HEX_DB_PATH on import: point them at the copy.
    os.environ["HEX_DB_PATH"] = copy
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        db = sqlite3.connect(copy)
        try:
            problems = check_database(
                db, args.champion, simulate=not args.no_simulate,
                walk=not args.no_dungeons)
        finally:
            db.close()
    finally:
        os.unlink(copy)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
