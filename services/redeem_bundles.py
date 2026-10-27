"""Redeem-code bundles: data-driven rewards such as the Kickstarter tiers.

Bundles live in ``redeem_bundles.json`` at the repository root.  A player can
redeem each bundle code once (``redeem_code_claims``).  ``includes`` merges
other bundles: lists are unioned and counts take the maximum, so a tier that
includes the tier below keeps its own totals.
"""

import json
import os
import random
from functools import lru_cache

from db import log_req

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "redeem_bundles.json")


@lru_cache(maxsize=None)
def load_bundles():
    """Return ``{CODE: bundle}`` from redeem_bundles.json (empty if missing)."""
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH) as f:
        data = json.load(f)
    return {code.upper(): bundle for code, bundle in data.items()
            if not code.startswith("_")}


def is_bundle_code(code):
    return str(code or "").strip().upper() in load_bundles()


def resolve(code, _seen=None):
    """Merge a bundle with everything it includes into one flat bundle."""
    code = str(code).strip().upper()
    bundles = load_bundles()
    seen = _seen if _seen is not None else set()
    if code in seen or code not in bundles:
        return _empty()
    seen.add(code)
    own = bundles[code]
    merged = _empty()
    for included in own.get("includes", ()):
        _merge(merged, resolve(included, seen))
    _merge(merged, own)
    merged["title"] = own.get("title", code)
    return merged


def _empty():
    return {"boosters": {}, "starter_decks": [], "cards": {}, "items": [],
            "random_equipment": None, "all_cards": 0, "all_equipment": False,
            "gift_codes": []}


def _merge(into, bundle):
    for name, count in bundle.get("boosters", {}).items():
        into["boosters"][name] = max(into["boosters"].get(name, 0), int(count))
    for key in ("starter_decks", "items", "gift_codes"):
        for value in bundle.get(key, ()):
            if value not in into[key]:
                into[key].append(value)
    cards = bundle.get("cards", ())
    if isinstance(cards, dict):     # an already resolved bundle
        cards = [dict(card, guid=guid) for guid, card in cards.items()]
    for card in cards:
        guid = card["guid"].lower()
        qty = max(int(card.get("qty", 1)), into["cards"].get(guid, {}).get("qty", 0))
        into["cards"][guid] = {"name": card.get("name", guid), "qty": qty}
    equipment = bundle.get("random_equipment")
    if equipment and int(equipment.get("count", 0)) > int(
            (into["random_equipment"] or {}).get("count", 0)):
        into["random_equipment"] = equipment
    into["all_cards"] = max(into["all_cards"], int(bundle.get("all_cards", 0)))
    into["all_equipment"] = into["all_equipment"] or bool(
        bundle.get("all_equipment"))


def _set_guid(set_name):
    from gamedata import DEFAULT_RECORD_STORE
    for record in DEFAULT_RECORD_STORE.load("CardSetTemplate"):
        if str(record.field("m_Name") or "") == set_name:
            return record.guid
    return None


def _inventory_item_guid(name):
    """Resolve an InventoryItemData name, preferring Kickstarter-labeled items."""
    from gamedata import DEFAULT_RECORD_STORE
    matches = [record for record in DEFAULT_RECORD_STORE.load("InventoryItemData")
               if str(record.field("m_Name") or "") == name]
    if len(matches) > 1:
        labeled = [r for r in matches
                   if "kickstarter" in str(r.field("m_Description") or "").lower()]
        matches = labeled or matches
    return sorted(r.guid for r in matches)[0] if matches else None


def claim(conn, user_id, code):
    """Record a claim; False when this player already redeemed the code."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO redeem_code_claims (user_id, code) VALUES (?, ?)",
        (user_id, str(code).strip().upper()))
    return cursor.rowcount == 1


def apply_bundle(conn, user_id, code, rng=random):
    """Grant a resolved bundle in the caller's transaction.

    Returns ``{"title", "card_copies", "inventory_updates", "decks",
    "summary"}`` where ``inventory_updates`` are ``(template_guid, client_uid,
    total_quantity)`` rows for InventoryUpdated pushes.  Unknown names are
    logged and skipped so one stale entry does not void the whole code.
    """
    from profile_db import (db_add_collection, db_insert_card_instance,
                            db_next_card_instance_for_user, db_inventory_item,
                            db_next_inventory_client_uid,
                            db_upsert_inventory_item)
    from pvp_db import db_pvp_booster_card_guids
    from services.chest_loot import booster_pack_pool, chest_loot_pool
    from services.store import _STARTER_DECKS, _grant_deck_to_player

    bundle = resolve(code)
    summary = []
    inventory = {}

    def add_inventory(template_guid, quantity):
        inventory[template_guid] = inventory.get(template_guid, 0) + quantity

    for set_name, count in bundle["boosters"].items():
        set_guid = _set_guid(set_name)
        packs = booster_pack_pool(conn, set_guid) if set_guid else []
        if not packs:
            log_req(f"    Bundle {code}: no booster pack for set '{set_name}'")
            continue
        add_inventory(packs[0], count)
        summary.append(f"{count} {set_name} Booster Packs")

    for name in bundle["items"]:
        guid = _inventory_item_guid(name)
        if not guid:
            log_req(f"    Bundle {code}: unknown inventory item '{name}'")
            continue
        add_inventory(guid, 1)
        summary.append(name)

    equipment = bundle["random_equipment"]
    if equipment:
        set_guid = _set_guid(equipment.get("set", "")) or ""
        pool = [guid for rarity, guids in chest_loot_pool(conn, set_guid).items()
                if rarity in equipment.get("rarities", ()) for guid in guids]
        if pool:
            for guid in rng.choices(pool, k=int(equipment["count"])):
                add_inventory(guid, 1)
            summary.append(f"{equipment['count']} random "
                           f"{'/'.join(equipment['rarities'])} equipment")
    if bundle["all_equipment"]:
        rows = conn.execute(
            "SELECT guid FROM equipment_templates WHERE is_live=1").fetchall()
        for (guid,) in rows:
            add_inventory(guid, 1)
        summary.append(f"all {len(rows)} pieces of equipment")

    next_uid = db_next_inventory_client_uid(user_id, conn=conn)
    inventory_updates = []
    for template_guid, quantity in inventory.items():
        row = db_inventory_item(user_id, template_guid, conn=conn)
        candidate = next_uid if not row or not row[2] else 0
        client_uid = db_upsert_inventory_item(
            user_id, template_guid, quantity, client_item_uid=candidate,
            conn=conn)
        if candidate:
            next_uid += 1
        inventory_updates.append(
            (template_guid, client_uid, int(row[1] or 0) + quantity if row else quantity))

    cards = dict((guid, card["qty"]) for guid, card in bundle["cards"].items())
    if bundle["all_cards"]:
        for guid in db_pvp_booster_card_guids(conn=conn):
            cards[guid] = max(cards.get(guid, 0), bundle["all_cards"])
        summary.append(f"{bundle['all_cards']}x every PvP card")
    known = {row[0] for row in conn.execute(
        "SELECT guid FROM card_templates WHERE guid IN (%s)"
        % ",".join("?" * len(cards)), list(cards)).fetchall()} if cards else set()
    next_id = db_next_card_instance_for_user(user_id, conn=conn)
    card_copies = 0
    for guid, quantity in cards.items():
        if guid not in known:
            log_req(f"    Bundle {code}: unknown card template {guid}")
            continue
        db_add_collection(user_id, guid, quantity, conn=conn)
        for _ in range(quantity):
            db_insert_card_instance(user_id, next_id, guid, conn=conn)
            next_id += 1
        card_copies += quantity
    summary.extend(f"{card['qty']}x {card['name']}"
                   for guid, card in bundle["cards"].items() if guid in known)

    decks = []
    for race in bundle["starter_decks"]:
        deck = _STARTER_DECKS.get(race)
        if not deck:
            log_req(f"    Bundle {code}: unknown starter deck '{race}'")
            continue
        _grant_deck_to_player(user_id, deck["cards"], f"{race} Starter Deck",
                              conn=conn)
        decks.append(race)
    if decks:
        summary.append("Starter Decks: " + ", ".join(decks))
    for gift in bundle["gift_codes"]:
        summary.append(f"gift code for friends: {gift}")

    return {"title": bundle["title"], "card_copies": card_copies,
            "inventory_updates": inventory_updates, "decks": decks,
            "summary": summary}
