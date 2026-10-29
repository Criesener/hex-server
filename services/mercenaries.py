"""Mercenary parties: profile flags, party persistence, and wire encoding.

Mercenaries join a PvE champion's party.  The client shows as many open
party slots as the ``CAMP_PARTYCAP`` profile flag's Progress (plus talent
bonuses), up to three (four for Humans).  The retail servers raised the cap
when the AZ2 mercenary recruitment encounters were won.

Profile data reaches the client as standalone ObjFmt objects in the login
profile stream (``PlayerProfile.HandleProfileStream``): ``List<FlagData>``
and ``List<CampSysGeneral+Party+ChampionParty>``.  The campaign service
answers ``partysave`` with the saved ChampionParty as JSON.
"""

import io
import json
import struct
import threading
from binascii import hexlify

PARTY_CAP_FLAG = "CAMP_PARTYCAP"
# The flag's Maximum.  The client adds one free slot per point of Progress
# without capping it (UIPartyMercenarySelector.LoadPartyMembers), so the
# server caps the earned slots itself.
MAX_PARTY_CAP = 4
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
FLAG_DATA_TYPE = "Reckoning.Profile.Messages.FlagData"
PARTY_TYPE = "Game.Shared.Campaign.Messages.CampSysGeneral+Party+ChampionParty"
PARTY_MEMBER_TYPE = PARTY_TYPE + "+ChampionPartyMember"


def list_type(element_type):
    return f"System.Collections.Generic.List`1#{element_type}"


def max_party_slots(race):
    """Client UIPartyMercenarySelector.GetMaxPartyForRace (Human=1 -> 4)."""
    return 4 if int(race or 0) == 1 else 3


# ── ObjFmt list encoding ─────────────────────────────────────────────

class ObjFmtListWriter:
    """Encode a standalone ``List<T>`` exactly like ``encode_chest_list``.

    Fields are written as ``(name, kind, value)`` tuples where kind is one of
    ``int``, ``uint``, ``ulong``, ``bool``, ``string``, ``rid`` (ResourceId),
    or ``list`` with value ``(element_type, [fields, ...])``.
    """

    PRIMITIVES = {"int": "System.Int32", "uint": "System.UInt32",
                  "ulong": "System.UInt64", "bool": "System.Boolean",
                  "string": "System.String", "bytes": "System.Byte[]"}

    def __init__(self, element_type, type_names=None):
        self.types = [list_type(element_type)] + list(type_names or [])
        self.sizes = []
        self.buf = io.BytesIO()

    def _w(self, text):
        self.buf.write(text.encode("utf-8"))

    def _sep(self):
        self.buf.write(b";")

    def _ft(self, name):
        if name not in self.types:
            self.types.append(name)
        return self.types.index(name)

    def _field(self, name, kind, value):
        start = self.buf.tell()
        self.sizes.append(0)
        idx = len(self.sizes) - 1
        if kind == "rid":
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft("Game.Shared.ResourceId"))); self._sep(); self._w("1"); self._sep()
            g_start = self.buf.tell()
            self.sizes.append(0)
            g_idx = len(self.sizes) - 1
            self._w("guid"); self._sep(); self._w(str(g_idx)); self._sep()
            self._w(str(self._ft("System.Guid"))); self._sep(); self._w("0"); self._sep()
            guid = str(value or ZERO_GUID).encode()
            self._w(str(len(guid))); self._sep(); self.buf.write(guid)
            self.sizes[g_idx] = self.buf.tell() - g_start
        elif kind == "list":
            element_type, elements = value
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(list_type(element_type)))); self._sep(); self._w("0"); self._sep()
            self._elements(element_type, elements)
        else:
            self._w(name); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(self.PRIMITIVES[kind]))); self._sep(); self._w("0"); self._sep()
            if kind == "bool":
                self._w("1" if value else "0")
            elif kind == "string":
                data = str(value or "").encode("utf-8")
                self._w(str(len(data))); self._sep(); self.buf.write(data)
            elif kind == "bytes":
                data = bytes(value or b"")
                self.buf.write(struct.pack("!I", len(data))); self.buf.write(data)
            else:
                fmt = {"int": "<i", "uint": "<I", "ulong": "<Q"}[kind]
                self._w(hexlify(struct.pack(fmt, int(value or 0))).decode("ascii")); self._sep()
        self.sizes[idx] = self.buf.tell() - start

    def _elements(self, element_type, elements):
        self._w(str(len(elements))); self._sep()
        for i, fields in enumerate(elements):
            start = self.buf.tell()
            self.sizes.append(0)
            idx = len(self.sizes) - 1
            self._w(str(i)); self._sep(); self._w(str(idx)); self._sep()
            self._w(str(self._ft(element_type))); self._sep(); self._w(str(len(fields))); self._sep()
            for field in fields:
                self._field(*field)
            self.sizes[idx] = self.buf.tell() - start

    def encode(self, element_type, elements):
        self.sizes.append(0)
        self._w(""); self._sep(); self._w("0"); self._sep()
        self._w(str(self._ft(self.types[0]))); self._sep(); self._w("0"); self._sep()
        start = self.buf.tell()
        self.sizes.append(0)
        self._elements(element_type, elements)
        self.sizes[1] = self.buf.tell() - start
        self.sizes[0] = self.buf.tell()
        self._w(";".join(self.types)); self.buf.write(b"\n")
        self._w(";".join(str(size) for size in self.sizes))
        return self.buf.getvalue()


def encode_flag_list(flags):
    """flags: iterable of (name, progress, maximum, completed)."""
    elements = [[("Name", "string", name), ("Progress", "int", progress),
                 ("Maximum", "int", maximum), ("Completed", "bool", completed)]
                for name, progress, maximum, completed in flags]
    return ObjFmtListWriter(FLAG_DATA_TYPE).encode(FLAG_DATA_TYPE, elements)


def encode_party_list(parties):
    """parties: iterable of ChampionParty dicts (see ``normalize_party``)."""
    elements = []
    for party in parties:
        members = [[("Mercenary", "rid", member["Mercenary"]),
                    ("DeckTemplate", "ulong", member["DeckTemplate"]),
                    ("Upgrade", "int", member["Upgrade"])]
                   for member in party["Members"]]
        elements.append([("Id", "ulong", party["Id"]),
                         ("ChampionID", "ulong", party["ChampionID"]),
                         ("PlayerId", "ulong", party["PlayerId"]),
                         ("Members", "list", (PARTY_MEMBER_TYPE, members))])
    return ObjFmtListWriter(PARTY_TYPE).encode(PARTY_TYPE, elements)


# ── Party JSON ────────────────────────────────────────────────────────

def _guid(value):
    """Accept a ResourceId as a GUID string or {"m_Guid"/"guid": ...}."""
    if isinstance(value, dict):
        for key in ("m_Guid", "guid", "Guid", "Id"):
            if key in value:
                return _guid(value[key])
        return ZERO_GUID
    return str(value or ZERO_GUID).lower()


def normalize_party(party):
    """Return a ChampionParty dict with plain types for storage/encoding."""
    party = party or {}
    members = []
    for member in party.get("Members") or []:
        members.append({
            "Mercenary": _guid(member.get("Mercenary")),
            "DeckTemplate": int(member.get("DeckTemplate") or 0),
            "Upgrade": int(member.get("Upgrade") or 0),
        })
    return {"Id": int(party.get("Id") or 0),
            "ChampionID": int(party.get("ChampionID") or 0),
            "PlayerId": int(party.get("PlayerId") or 0),
            "Members": members}


# ── Battle ────────────────────────────────────────────────────────────

def mercenary_template_guid(item_guid):
    """Map an InventoryMercenaryData GUID to its MercenaryTemplate GUID."""
    from gamedata import DEFAULT_RECORD_STORE
    item = DEFAULT_RECORD_STORE.get("InventoryItemData", str(item_guid).lower())
    if item is None:
        return None
    return _guid(item.field("m_MercenaryTemplateId")) or None


def battle_mercenary(db, user_id, champion_id, item_guid):
    """Resolve the mercenary a champion chose for an encounter.

    ``item_guid`` is the InventoryMercenaryData GUID the client sends as
    ``merc=<guid>`` in the campaign 'start' event.  Returns the champion
    template, name, starting health, the party member's deck cards and the
    equipment its party deck template equips, or None when the mercenary has
    no saved deck.
    """
    from services import deck_templates
    from services.equipment import sanitize_deck_equipment
    item_guid = str(item_guid or "").lower()
    if not item_guid or item_guid == ZERO_GUID:
        return None
    member = None
    for party in get_parties(db, user_id):
        if party["ChampionID"] == int(champion_id or 0):
            member = next((m for m in party["Members"]
                           if m["Mercenary"] == item_guid), None)
    if not member or not member["DeckTemplate"]:
        return None
    template = deck_templates.get_template(db, user_id, member["DeckTemplate"])
    champion_guid = mercenary_template_guid(item_guid)
    if not template or not champion_guid:
        return None
    row = db.execute(
        "SELECT name, starting_health FROM champion_templates_extended WHERE guid=?",
        (champion_guid,)).fetchone()
    if not row:
        return None
    # The template's Equip slots are played like a deck's equipment
    # (HEX Revival Decks.FromTemplate), limited to items the player owns.
    equipment = sanitize_deck_equipment(
        db, user_id, deck_templates.template_equipment(template[2]))
    return {"champion_guid": champion_guid, "name": row[0],
            "starting_health": int(row[1] or 20),
            "deck_cards": deck_templates.template_cards(template[2]),
            "equipment": equipment}


# ── Party passives ────────────────────────────────────────────────────

_TEMPLATES = None
_TEMPLATES_LOCK = threading.Lock()


def _load_mercenary_templates():
    """Read MercenaryTemplate records: GUID -> party passives and upgrade.

    The export writes ``m_ShardAlignment`` with unquoted keys, which the
    strict ``RecordStore`` decoder rejects for most mercenaries; the seed
    parser tolerates that form.
    """
    from gamedata import DEFAULT_RECORD_STORE
    from AssetExtraction.gamedata_seed import parse_record
    templates = {}
    path = DEFAULT_RECORD_STORE.records_dir / "MercenaryTemplate.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return templates
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            if value.lstrip().startswith("$$$"):
                continue
            value = parse_record(value)
        if not isinstance(value, dict):
            continue
        guid = _guid(value.get("m_Id"))
        if guid == ZERO_GUID:
            continue
        abilities = [_guid(entry.get("m_CardAbilityId"))
                     for entry in value.get("m_PartyPassiveAbilities") or []
                     if isinstance(entry, dict)]
        upgrade = _guid(value.get("m_UpgradeTemplateId"))
        templates[guid] = {
            "name": value.get("m_Name") or "",
            "party_abilities": [a for a in abilities if a != ZERO_GUID],
            "upgrade": upgrade if upgrade != ZERO_GUID else None,
        }
    return templates


def mercenary_templates():
    """Cached MercenaryTemplate GUID -> {name, party_abilities, upgrade}."""
    global _TEMPLATES
    if _TEMPLATES is None:
        with _TEMPLATES_LOCK:
            if _TEMPLATES is None:
                _TEMPLATES = _load_mercenary_templates()
    return _TEMPLATES


def upgraded_template_guid(template_guid, upgrade):
    """Follow ``m_UpgradeTemplateId`` ``upgrade`` times
    (MercenaryTemplate.GetUpgradeTemplate); stop at the last level."""
    templates = mercenary_templates()
    guid = str(template_guid or "").lower()
    if guid not in templates:
        return None
    for _ in range(max(0, int(upgrade or 0))):
        nxt = templates[guid]["upgrade"]
        if not nxt or nxt not in templates:
            break
        guid = nxt
    return guid


def party_member_templates(db, user_id, champion_id):
    """The champion's party as MercenaryTemplate GUIDs at their upgrade level.

    This is the server's ``DeckChampInfo.PartyMembers``: the campaign server
    hands it to the game session, which adds every member's party passives
    to the fighter (AuthoritativeSessionBase.InstantiateChampionCard /
    InstantiateMercenaryCard).  The client never receives the list.
    """
    members = []
    for party in get_parties(db, user_id):
        if party["ChampionID"] != int(champion_id or 0):
            continue
        for member in party["Members"]:
            template = mercenary_template_guid(member["Mercenary"])
            template = upgraded_template_guid(template, member["Upgrade"])
            if template:
                members.append(template)
    return members


def party_passive_ability_guids(db, user_id, champion_id):
    """Party passive AbilityTemplate GUIDs of a champion's party, in order.

    Abilities missing from the client's AbilityTemplates (unreleased
    mercenaries) are skipped, like the seeded champion abilities.
    """
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    templates = mercenary_templates()
    abilities = []
    for template in party_member_templates(db, user_id, champion_id):
        for ability in templates[template]["party_abilities"]:
            if ability in abilities:
                continue
            if ability_graph(DEFAULT_RECORD_STORE, ability) is None:
                continue
            abilities.append(ability)
    return abilities


# ── Party slots ───────────────────────────────────────────────────────

def owned_mercenary_count(db, user_id):
    """Distinct mercenaries (InventoryMercenaryData items) the player owns."""
    from gamedata import DEFAULT_RECORD_STORE
    count = 0
    for (template_guid,) in db.execute(
            "SELECT DISTINCT template_guid FROM player_inventory "
            "WHERE user_id=? AND quantity>0", (user_id,)).fetchall():
        item = DEFAULT_RECORD_STORE.get("InventoryItemData",
                                        str(template_guid or "").lower())
        if item is not None and item.field("m_MercenaryTemplateId"):
            count += 1
    return count


def party_cap_override(db, user_id):
    row = db.execute("SELECT slots FROM party_cap_overrides WHERE user_id=?",
                     (user_id,)).fetchone()
    return int(row[0]) if row else None


def set_party_cap_override(db, user_id, slots):
    """Pin CAMP_PARTYCAP to ``slots``; None returns it to owned mercenaries."""
    if slots is None:
        db.execute("DELETE FROM party_cap_overrides WHERE user_id=?", (user_id,))
    else:
        db.execute(
            "INSERT INTO party_cap_overrides (user_id, slots) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET slots=excluded.slots",
            (user_id, max(0, min(MAX_PARTY_CAP, int(slots)))))
    return sync_party_cap(db, user_id)


def party_cap(db, user_id):
    """CAMP_PARTYCAP: the override, else one slot per mercenary owned (max 4)."""
    override = party_cap_override(db, user_id)
    if override is not None:
        return override
    return min(MAX_PARTY_CAP, owned_mercenary_count(db, user_id))


def sync_party_cap(db, user_id):
    """Store the current CAMP_PARTYCAP flag and return its slot count.

    A player without mercenaries and without a flag keeps no flag at all,
    so the login flag stream is unchanged for them.
    """
    slots = party_cap(db, user_id)
    has_flag = db.execute(
        "SELECT 1 FROM reckoning_flags WHERE user_id=? AND name=?",
        (user_id, PARTY_CAP_FLAG)).fetchone()
    if slots or has_flag:
        set_flag(db, user_id, PARTY_CAP_FLAG, slots, MAX_PARTY_CAP)
    return slots


# ── Persistence ───────────────────────────────────────────────────────

def get_flags(db, user_id):
    """Profile flags live in the shared ``reckoning_flags`` table, which the
    login stream sends as the one List<FlagData>."""
    return [tuple(row) for row in db.execute(
        "SELECT name, progress, maximum, completed FROM reckoning_flags "
        "WHERE user_id=? ORDER BY name", (user_id,)).fetchall()]


def set_flag(db, user_id, name, progress, maximum=0, completed=False):
    from profile_db import db_set_reckoning_flag
    db_set_reckoning_flag(user_id, name, progress, maximum, completed,
                          conn=db)


def get_parties(db, user_id):
    parties = []
    for (party_json,) in db.execute(
            "SELECT party_json FROM champion_parties WHERE user_id=? "
            "ORDER BY champion_id", (user_id,)).fetchall():
        try:
            parties.append(normalize_party(json.loads(party_json)))
        except (TypeError, ValueError):
            continue
    return parties


def save_party(db, user_id, party):
    """Store a party for the player and return the normalized copy."""
    party = normalize_party(party)
    party["PlayerId"] = party["PlayerId"] or int(user_id)
    if not party["Id"]:
        party["Id"] = party["ChampionID"]
    db.execute(
        "INSERT INTO champion_parties (champion_id, user_id, party_json) "
        "VALUES (?,?,?) ON CONFLICT(champion_id) DO UPDATE SET "
        "user_id=excluded.user_id, party_json=excluded.party_json",
        (party["ChampionID"], user_id, json.dumps(party)))
    return party
