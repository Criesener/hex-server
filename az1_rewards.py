"""AZ1 map reward table (``az1_rewards.json``, issue #38 part B).

The table is static game data: it is parsed once under a lock and shared
read-only by every handler thread.  ``apply_scene_rewards`` turns a table
entry into the ``encounter_scenes.rewards_json`` shape that
``campaign._apply_encounter_end_rewards`` evaluates, so the seeding in
``static.ensure_schema`` stays an idempotent refresh for existing databases.
"""

import copy
import json
import random
from pathlib import Path
from threading import Lock

AZ1_REWARDS_PATH = Path(__file__).resolve().parent / "az1_rewards.json"

_LOCK = Lock()
_TABLE = None


def load_table():
    """The parsed reward table (a private copy is never needed: read-only)."""
    global _TABLE
    with _LOCK:
        if _TABLE is None:
            try:
                _TABLE = json.loads(AZ1_REWARDS_PATH.read_text())
            except (OSError, ValueError):
                _TABLE = {}
        return _TABLE


def _pack_guid(table, key):
    return (table.get("packs") or {}).get(str(key or ""), key)


def battle_entry(scene_name, table=None):
    """The table entry of one EncounterScene name, or None."""
    table = load_table() if table is None else table
    name = str(scene_name or "")
    for entry in table.get("battles") or []:
        if entry.get("scene") == name:
            return entry
    for entry in table.get("battles") or []:
        suffix = entry.get("name_suffix")
        if suffix and name.endswith(suffix):
            return entry
    return None


def _first_win_record(table, item):
    record = {key: copy.deepcopy(value) for key, value in item.items()
              if not key.startswith("_") and key != "pack"}
    if item.get("pack"):
        record["chest_guid"] = _pack_guid(table, item["pack"])
    if record.get("from_pack"):
        record["from_pack"] = _pack_guid(table, record["from_pack"])
    record["one_time"] = True
    return record


def apply_scene_rewards(scene_name, rewards, table=None):
    """Return *rewards* (a rewards_json dict) with the table's payout.

    Gold and XP are one repeatable record; every first-win record is
    one-time.  A conditional record already on the scene (the Tamed capture)
    is kept right after the currency so existing claim indices stay valid.
    Shroom Haus scenes get their node's card set.  Scenes the table does not
    name are returned unchanged.
    """
    table = load_table() if table is None else table
    rewards = dict(rewards or {})
    haus = (table.get("shroom_hauses") or {}).get(str(scene_name or ""))
    cards = (table.get("shroom_sets") or {}).get(haus) if haus else None
    if cards:
        rewards["card_choice"] = copy.deepcopy(cards)
    entry = battle_entry(scene_name, table)
    if entry is None:
        return rewards
    conditional = [record for record in rewards.get("end_of_game_rewards")
                   or [] if isinstance(record, dict) and
                   record.get("end_of_game_condition")]
    for key in ("gold", "xp", "one_time", "end_of_game_rewards",
                "end_of_game_condition", "card_guid", "quantity",
                "chest_guid", "chest_template", "pack_guid", "items",
                "item_guid"):
        rewards.pop(key, None)
    gold, xp = int(entry.get("gold") or 0), int(entry.get("xp") or 0)
    currency = ([{"gold": gold, "xp": xp, "one_time": False}]
                if gold or xp else [])
    first_win = [_first_win_record(table, item)
                 for item in entry.get("first_win") or []]
    if conditional or first_win:
        rewards["end_of_game_rewards"] = currency + conditional + first_win
    elif currency:
        rewards.update(currency[0])
    return rewards


def random_equipment_specs(record, rng=None):
    """Item specs for a ``random_equipment`` record (empty otherwise).

    The pieces are drawn from the pack's observed equipment drop table
    (``pack_loot_tables.json``), weighted by its drop rates.
    """
    try:
        count = max(0, int(record.get("random_equipment") or 0))
    except (TypeError, ValueError):
        count = 0
    if not count:
        return []
    from az1_pack import pack_loot_table
    table = pack_loot_table(record.get("from_pack"))
    if table is None or not table.equipment:
        return []
    rng = rng or random
    entries = list(table.equipment)
    picks = rng.choices(entries, weights=[entry.weight for entry in entries],
                        k=count)
    return [{"guid": entry.guid, "quantity": 1} for entry in picks]


def stardust_rarity(template_guid):
    """The Stardust rarity of an InventoryStardust GUID, or None."""
    from db import STARDUST_TEMPLATES
    guid = str(template_guid or "").lower()
    return next((rarity for rarity, value in STARDUST_TEMPLATES.items()
                 if value == guid), None)


def event_cost(event_name, table=None):
    """Gold a campaign script event costs (0 if it is free)."""
    table = load_table() if table is None else table
    cost = (table.get("event_costs") or {}).get(str(event_name or "")) or {}
    try:
        return max(0, int(cost.get("gold") or 0))
    except (TypeError, ValueError):
        return 0
