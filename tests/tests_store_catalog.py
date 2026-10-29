"""Store catalog (issue #42): the HEX Revival product range priced in gold.

``store_catalog.json`` is synced into ``store_items`` on every boot; deck
products grant their DeckTemplate deck, battleboards become inventory items,
and a deck's chosen battleboard is carried in the deck encoding.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import db
import static
from encoder import deck_cosmetics, encode_deck_bits_fields, encode_store_response
from profile_db import (db_get_store_items, db_inventory_item,
                        db_set_inventory_client_uid, db_add_inventory)
from services import store
from services.store import apply_purchase

BASIC_BOARD = "3da4e3b7-e8e9-416a-b36c-e56cbc0aec47"
TEST_CHEST = "a9ae9af2-e27a-48e0-9cd2-490d252fffe4"
COLLECTOR_LIBRARIAN = "62abd6a9-f58c-4a27-9d93-98cde9d093fb"
BADLANDS_BOARD = "10b0e374-d304-4dad-b493-7ba4493c76be"
SET1_BOOSTER = "a8b78207-686a-4994-b6cd-4548d1349841"


def _catalog():
    with open(static.STORE_CATALOG_PATH, encoding="utf-8") as handle:
        return json.load(handle)["items"]


def _item_id(template_guid):
    row = db._db.execute("SELECT id FROM store_items WHERE template_guid=?",
                         (template_guid,)).fetchone()
    assert row, f"{template_guid} missing from store_items"
    return int(row[0])


def _new_user(user_id, gold):
    db._db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db._db.execute(
        "INSERT INTO users (id, name, gold, platinum) VALUES (?,?,?,?)",
        (user_id, f"StoreTester{user_id}", gold, 0))
    db._db.commit()
    return user_id


def _gold(user_id):
    return int(db._db.execute("SELECT gold FROM users WHERE id=?",
                              (user_id,)).fetchone()[0])


def _inventory_records():
    from AssetExtraction.gamedata_seed import (load_records_text, nested_guid,
                                               records)
    data = load_records_text()
    return {nested_guid(record, "m_Id"): record
            for _, record in records(data, "InventoryItemData")[0]}


def test_sync_rebuilds_existing_store_with_contiguous_ids():
    connection = sqlite3.connect(":memory:")
    # A database from before #42: old columns, gaps, stale rows.
    connection.execute(
        "CREATE TABLE store_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "template_guid TEXT NOT NULL, name TEXT NOT NULL, short_desc TEXT, "
        "price INTEGER DEFAULT 100, currency TEXT DEFAULT 'Gold', "
        "store_tab TEXT DEFAULT 'ShopBoosterTab')")
    connection.executemany(
        "INSERT INTO store_items (id, template_guid, name, price, currency, "
        "store_tab) VALUES (?,?,?,?,?,?)",
        [(3, SET1_BOOSTER, "Set 1", 100, "Gold", "ShopBoosterTab"),
         (9, TEST_CHEST, "Common Chest (Test)", 10, "Gold", "special"),
         (70, COLLECTOR_LIBRARIAN, "Collector", 1000, "Platinum",
          "collectordeck")])
    static._ensure_column(connection, "store_items", "hide_in_store",
                          "INTEGER NOT NULL DEFAULT 0")
    static._ensure_column(connection, "store_items", "purchase_limit",
                          "INTEGER NOT NULL DEFAULT -1")
    assert static.sync_store_catalog(connection) is True
    rows = connection.execute(
        "SELECT id, template_guid, price, currency FROM store_items "
        "ORDER BY id").fetchall()
    catalog = _catalog()
    assert [row[0] for row in rows] == list(range(1, len(catalog) + 1))
    assert [row[1] for row in rows] == [e["template_guid"] for e in catalog]
    assert TEST_CHEST not in {row[1] for row in rows}
    assert {row[3] for row in rows} == {"Gold"}
    # Unchanged catalog: the next boot leaves the table alone.
    assert static.sync_store_catalog(connection) is False
    # The live test database was synced by ensure_schema the same way.
    live = [row[0] for row in db._db.execute(
        "SELECT id FROM store_items ORDER BY id")]
    assert live == list(range(1, len(catalog) + 1))


def test_every_catalog_product_exists_in_game_data():
    inventory = _inventory_records()
    expected_type = {"cardpack": ("InventoryCardPack",),
                     "collectordeck": ("InventoryStarterPack",),
                     "special": ("InventoryCardPack", "InventoryGameboardData",
                                 "InventoryTreasureChest")}
    catalog = _catalog()
    for entry in catalog:
        record = inventory.get(entry["template_guid"])
        assert record, f"{entry['name']} has no InventoryItemData record"
        assert str(record["_t"]).endswith(expected_type[entry["tab"]]), \
            (entry["name"], record["_t"])
    by_type = {}
    for entry in catalog:
        kind = str(inventory[entry["template_guid"]]["_t"]).rsplit(".", 1)[-1]
        by_type.setdefault((kind, entry["tab"]), []).append(entry)
    boards = by_type[("InventoryGameboardData", "special")]
    assert len(boards) == 14 and all(e["price"] == 3000 for e in boards)
    assert BASIC_BOARD not in {e["template_guid"] for e in boards}
    packs = by_type[("InventoryCardPack", "cardpack")]
    kinds = [inventory[e["template_guid"]].get("m_CardPackType") for e in packs]
    assert kinds.count("BoosterPack") == 9 and kinds.count("PrimalPack") == 9
    for entry, kind in zip(packs, kinds):
        assert entry["price"] == (200 if kind == "BoosterPack" else 3000)
    named_decks = [r for r in inventory.values()
                   if str(r["_t"]).endswith("InventoryStarterPack")
                   and r.get("m_Name")]
    decks = by_type[("InventoryStarterPack", "collectordeck")]
    assert len(decks) == len(named_decks)
    collectors = [e for e in decks if e["name"].startswith("Collector's Deck")]
    assert len(collectors) == 8
    assert all(e["price"] == 2500 and e["purchase_limit"] == 1
               for e in collectors)


def test_tabs_and_flags_reach_the_client():
    items = db_get_store_items()
    assert {item["t"] for item in items} == {"cardpack", "collectordeck",
                                            "special"}
    assert not any(item["hide"] for item in items)
    encoded = encode_store_response(items)
    assert b"ShopBoosterTab" not in encoded
    raw = json.dumps({"Price": 200, "ItemType": "Inventory",
                      "CurrencyType": "Gold",
                      "ItemTemplateId": "{" + SET1_BOOSTER.upper() + "}",
                      "Name": items[0]["n"], "StoreTab": "cardpack"})
    assert items[0]["template_guid"] == SET1_BOOSTER
    assert raw.encode("utf-8") in encoded
    # The encoded id is the store_items row id, not the list position.
    sample = [dict(items[0], id=42, limit=1, hide=True)]
    one = encode_store_response(sample)
    assert b"2a00000000000000" in one
    assert b"HideInStore;" in one


def test_store_deck_products_resolve_to_card_lists():
    decks = [item for item in db_get_store_items()
             if item["t"] == "collectordeck"]
    known_cards = {row[0] for row in db._db.execute(
        "SELECT guid FROM card_templates")}
    missing = [item["n"] for item in decks
               if not store._STORE_DECK_CARDS.get(item["template_guid"])]
    assert not missing, f"deck products without a card list: {missing}"
    for item in decks:
        cards = store._STORE_DECK_CARDS[item["template_guid"]]["cards"]
        assert sum(count for _, count in cards) >= 40, item["n"]
        unknown = [guid for guid, _ in cards if guid not in known_cards]
        assert not unknown, (item["n"], unknown[:3])


def test_buying_a_collectors_deck_grants_the_deck_once():
    user_id = _new_user(94201, 6000)
    item_id = _item_id(COLLECTOR_LIBRARIAN)
    result = apply_purchase(db._db, user_id, item_id, 1)
    db._db.commit()
    assert result["success"] and result["deck_granted"], result
    assert _gold(user_id) == 6000 - 2500
    deck = db._db.execute(
        "SELECT deck_name, cards, pvp_champion_guid FROM decks "
        "WHERE user_id=?", (user_id,)).fetchone()
    assert deck, "no deck granted"
    assert deck[0] == "Collector's Deck - The Librarian"
    instance_ids = json.loads(deck[1])
    assert len(instance_ids) == 60
    owned = db._db.execute(
        "SELECT COUNT(*) FROM card_instances WHERE user_id=?",
        (user_id,)).fetchone()[0]
    assert owned == 60
    assert deck[2], "deck keeps its DeckTemplate champion"
    assert db_inventory_item(user_id, COLLECTOR_LIBRARIAN)
    # Purchase limit 1: the second purchase is rejected and costs nothing.
    again = apply_purchase(db._db, user_id, item_id, 1)
    db._db.commit()
    assert not again["success"] and again["error_code"] == 6, again
    assert _gold(user_id) == 6000 - 2500


def test_buying_a_battleboard_adds_it_to_inventory():
    user_id = _new_user(94202, 3500)
    item_id = _item_id(BADLANDS_BOARD)
    result = apply_purchase(db._db, user_id, item_id, 1)
    db._db.commit()
    assert result["success"] and not result["deck_granted"], result
    assert _gold(user_id) == 500
    row = db_inventory_item(user_id, BADLANDS_BOARD)
    assert row and int(row[1]) == 1 and int(row[2]) == 1000 + item_id
    assert result["granted_list"] == [(BADLANDS_BOARD, 1000 + item_id, 1)]


def test_insufficient_gold_is_rejected_without_changes():
    user_id = _new_user(94203, 2999)
    result = apply_purchase(db._db, user_id, _item_id(BADLANDS_BOARD), 1)
    db._db.commit()
    assert not result["success"] and result["error_code"] == 2, result
    assert _gold(user_id) == 2999
    assert db_inventory_item(user_id, BADLANDS_BOARD) is None


def test_store_uid_already_taken_gets_a_fresh_inventory_uid():
    user_id = _new_user(94204, 1000)
    item_id = _item_id(SET1_BOOSTER)
    # Another product already holds this store UID (older catalog order).
    db_add_inventory(user_id, TEST_CHEST, 1, conn=db._db)
    db_set_inventory_client_uid(user_id, TEST_CHEST, 1000 + item_id,
                                conn=db._db)
    result = apply_purchase(db._db, user_id, item_id, 1)
    db._db.commit()
    assert result["success"], result
    uid = int(db_inventory_item(user_id, SET1_BOOSTER)[2])
    assert uid != 1000 + item_id and result["client_uid"] == uid


def test_deck_encoding_carries_the_chosen_battleboard():
    import io
    buf = io.BytesIO()
    sizes = [0]
    names = []

    def ft(name):
        if name not in names:
            names.append(name)
        return names.index(name)

    cosmetics = deck_cosmetics({"gameboard_guid": BADLANDS_BOARD})
    encode_deck_bits_fields(buf, sizes, ft, 7, "Board Deck", 7, 0,
                            {"main": [], "sideboard": []},
                            cosmetics=cosmetics)
    encoded = buf.getvalue()
    start = encoded.index(b"gameboard;")
    assert BADLANDS_BOARD.encode() in encoded[start:start + 120]
    # Tournament deck info reads the board from the saved deck.
    from gamemodes import tournament_engine
    _new_user(94205, 0)
    deck_id = db.db_save_deck(94205, "Board Deck", "[]",
                              gameboard_guid=BADLANDS_BOARD)
    data = tournament_engine._make_deck_data(deck_id)
    assert data[6]["gameboard"] == BADLANDS_BOARD


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                import traceback
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
    sys.exit(1 if failures else 0)
