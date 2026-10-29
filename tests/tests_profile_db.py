"""Focused tests for profile-domain persistence helpers."""

import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from profile_db import (db_card_instance_template, db_champion_last_deck,
                        db_consume_inventory, db_find_deck_owner,
                        db_insert_card_instance, db_reset_account,
                        db_set_champion_last_deck, db_upsert_inventory_item,
                        db_adjust_user_currency, db_get_or_create_user,
                        WELCOME_MAIL_BODY, WELCOME_MAIL_SUBJECT)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, gold INTEGER DEFAULT 0,
            platinum INTEGER DEFAULT 0, experience INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1, flags TEXT DEFAULT '{}',
            name TEXT, last_login TEXT, last_ip TEXT
        );
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            sender TEXT, subject TEXT, body TEXT,
            gold_delivered INTEGER DEFAULT 0,
            platinum_delivered INTEGER DEFAULT 0,
            attachments_json TEXT DEFAULT '[]', read_at TEXT,
            claimed_at TEXT
        );
        CREATE TABLE stardust (
            user_id INTEGER, rarity TEXT, quantity INTEGER,
            PRIMARY KEY (user_id, rarity)
        );
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            card_template_id TEXT, quantity INTEGER
        );
        CREATE TABLE player_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            template_guid TEXT, quantity INTEGER, client_item_uid INTEGER
        );
        CREATE TABLE decks (id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE champions (
            id INTEGER PRIMARY KEY, user_id INTEGER, last_deck_id INTEGER,
            pet_name TEXT
        );
        CREATE TABLE card_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            instance_id INTEGER, template_guid TEXT,
            is_extended_art INTEGER DEFAULT 0
        );
        CREATE TABLE card_templates (
            guid TEXT PRIMARY KEY, is_pve INTEGER DEFAULT 0
        );
        CREATE TABLE game_sessions (
            session_id INTEGER PRIMARY KEY, owner_uid TEXT,
            players_json TEXT
        );
        CREATE TABLE game_cards (
            user_id INTEGER, owner_user_id INTEGER, session_id INTEGER
        );
    """)
    return conn


def test_adjust_user_currency_is_atomic_and_connection_scoped():
    conn = make_db()
    conn.execute("INSERT INTO users(id, gold, platinum) VALUES (1, 10, 20)")
    conn.commit()
    assert db_adjust_user_currency(1, gold_delta=7, platinum_delta=-3,
                                   conn=conn) == (17, 17)
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (17, 17)
    # The helper must not commit a transaction supplied by the caller.
    conn.rollback()
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (10, 20)


def test_inventory_consume_and_upsert_are_connection_scoped():
    conn = make_db()
    conn.execute(
        "INSERT INTO player_inventory(user_id,template_guid,quantity,client_item_uid) "
        "VALUES (1,'pack',3,44)")
    assert db_consume_inventory(1, "pack", 2, conn=conn) == (44, 1)
    assert db_consume_inventory(1, "pack", 2, conn=conn) is None
    assert db_upsert_inventory_item(1, "reward", 1, 45, conn=conn) == 45
    assert db_upsert_inventory_item(1, "reward", 2, 46, conn=conn) == 45
    assert conn.execute(
        "SELECT quantity, client_item_uid FROM player_inventory "
        "WHERE user_id=1 AND template_guid='reward'").fetchone() == (3, 45)


def test_deck_champion_and_card_instance_helpers_preserve_ownership():
    conn = make_db()
    conn.execute("INSERT INTO decks(id,user_id) VALUES (7,11)")
    conn.execute("INSERT INTO champions(id,user_id,pet_name) VALUES (3,11,'Fox')")
    assert db_find_deck_owner(7, conn=conn) == 11
    assert db_set_champion_last_deck(3, 7, user_id=11, conn=conn) == 1
    assert db_set_champion_last_deck(3, 8, user_id=99, conn=conn) == 0
    assert tuple(db_champion_last_deck(3, conn=conn)) == (7, "Fox")
    db_insert_card_instance(11, 501, "template-guid", conn=conn)
    assert db_card_instance_template(11, 501, conn=conn) == "template-guid"


def test_reset_account_keeps_pve_and_extended_art_cards():
    """!account-cleanup removes only plain PvP copies from the collection.

    PvE printings and extended-art (alternate art) PvP copies are permanent
    account rewards; the reset must leave both their instances and their
    matching ``collections`` counts in place.
    """
    conn = make_db()
    conn.execute(
        "INSERT INTO users(id, gold, platinum, experience, level, flags) "
        "VALUES (1, 5, 6, 7, 3, '{\"x\":1}')")
    conn.executemany(
        "INSERT INTO card_templates(guid, is_pve) VALUES (?,?)",
        [("pvp-plain", 0), ("pvp-alt", 0), ("pve-card", 1)])
    conn.executemany(
        "INSERT INTO card_instances(user_id, instance_id, template_guid, "
        "is_extended_art) VALUES (1,?,?,?)",
        [(10, "pvp-plain", 0), (11, "pvp-plain", 0), (12, "pvp-alt", 1),
         (13, "pve-card", 0)])
    conn.executemany(
        "INSERT INTO collections(user_id, card_template_id, quantity) "
        "VALUES (1,?,?)",
        [("pvp-plain", 2), ("pvp-alt", 1), ("pve-card", 1)])

    db_reset_account(1, conn=conn)

    def instances(template):
        return conn.execute(
            "SELECT instance_id, is_extended_art FROM card_instances "
            "WHERE user_id=1 AND template_guid=? ORDER BY instance_id",
            (template,)).fetchall()

    def quantity(template):
        row = conn.execute(
            "SELECT quantity FROM collections "
            "WHERE user_id=1 AND card_template_id=?", (template,)).fetchone()
        return row[0] if row else None

    assert instances("pvp-plain") == []
    assert instances("pvp-alt") == [(12, 1)]
    assert instances("pve-card") == [(13, 0)]
    assert quantity("pvp-plain") is None
    assert quantity("pvp-alt") == 1
    assert quantity("pve-card") == 1
    # The rest of the reset still runs (currency back to the grant).
    assert conn.execute(
        "SELECT gold, platinum FROM users WHERE id=1").fetchone() == (
        10000, 10000)


def test_new_user_receives_only_public_command_welcome_mail():
    conn = make_db()
    with mock.patch("profile_db._db_layer.player_id_from_name",
                    return_value=77), \
            mock.patch("new_player.grant_new_player"):
        db_get_or_create_user("NewPlayer", conn=conn)
        db_get_or_create_user("NewPlayer", conn=conn)

    row = conn.execute(
        "SELECT subject, body FROM emails WHERE user_id=77").fetchone()
    assert row == (WELCOME_MAIL_SUBJECT, WELCOME_MAIL_BODY)
    assert conn.execute(
        "SELECT COUNT(*) FROM emails WHERE user_id=77").fetchone()[0] == 1
    assert "!help" in row[1]
    assert "!issue <title>" in row[1]
    assert "!gencard" not in row[1]
    assert "allowcon" not in row[1]
    assert "!greenlight" not in row[1]



def test_new_player_starts_with_only_the_basic_shards():
    """Issue #5: a fresh account has no Lixil, Heartsworn."""
    import db as db_layer
    import new_player
    user_id = db_get_or_create_user("ShardsOnlyPlayer")["id"]
    rows = db_layer._db.execute(
        "SELECT template_guid, COUNT(*) FROM card_instances WHERE user_id=? "
        "GROUP BY template_guid", (user_id,)).fetchall()
    assert dict(rows) == {guid: new_player.SHARD_QUANTITY
                          for guid in new_player.BASIC_SHARDS}, rows


# --- Issue #33: champion starter decks -----------------------------------

_STARTER_RACE = 3  # ERace.Coyotle


def _starter_cards():
    import hconnect_server
    return hconnect_server._STARTER_DECKS[
        hconnect_server._RACE_DECK_MAP[_STARTER_RACE]]["cards"]


def _add_champion(user_id, name):
    """Run the AddChampion (2033) handler; return the parsed response."""
    import zlib
    import hconnect_server
    from application.objfmt_wire import parse_datawrapper
    sent = []
    handler = hconnect_server.HCPHandler.__new__(hconnect_server.HCPHandler)
    handler.user_profile = {"id": user_id}
    handler.client_uid = 1
    handler.scnt = 0
    handler.sid = "test"
    handler.send = lambda headers, body=b"": sent.append(body)
    handler.push_cards_to_client = lambda *args, **kwargs: None
    handler._handle_service_request_legacy(
        "ServiceProfile", "0", 2033, 2, 1,
        "00000000-0000-0000-0000-000000000000", 0,
        {"ChampionName": name, "Race": ["ERace", _STARTER_RACE],
         "ChampionClass": ["EChampionClass", 2],
         "Gender": ["EGender", 2]}, b"")
    assert len(sent) == 1, len(sent)
    start = sent[0].index(bytes([0x1F, 0x8B, 0x08]))
    inner = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(sent[0][start:])
    return parse_datawrapper(inner, preserve_complex=True)


def _deck_row(deck_id):
    import db as db_layer
    import json
    row = db_layer._db.execute(
        "SELECT user_id, deck_name, cards FROM decks WHERE id=?",
        (deck_id,)).fetchone()
    return row[0], row[1], json.loads(row[2])


def _templates_of(user_id, instance_ids):
    return sorted(db_card_instance_template(user_id, iid) for iid in instance_ids)


def _expected_templates():
    return sorted(guid for guid, count in _starter_cards() for _ in range(count))


def varint_at(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]; pos += 1
        value |= (byte & 0x7F) << shift; shift += 7
        if byte < 0x80:
            return value, pos


def _encoded_deck_card_counts(data):
    """Card copies per deck in an EncodedDecks payload."""
    import struct
    _version, count = struct.unpack_from("<ii", data, 0)
    pos, totals = 8, []
    for _ in range(count):
        length = struct.unpack_from("<i", data, pos)[0]; pos += 4
        template, pos = data[pos:pos + length], pos + length
        tpos = 0
        name_len, tpos = varint_at(template, tpos)
        tpos += name_len + 32
        equipped, tpos = varint_at(template, tpos)
        for _ in range(equipped):
            _slot, tpos = varint_at(template, tpos); tpos += 16
        cards, tpos = varint_at(template, tpos)
        total = 0
        for _ in range(cards):
            tpos += 16
            copies, tpos = varint_at(template, tpos); total += copies
            tpos += 3
            gems, tpos = varint_at(template, tpos)
            for _ in range(gems):
                _gem, tpos = varint_at(template, tpos)
        totals.append(total)
        pos += 16 + 8 + 4
        for _ in range(3):
            _value, pos = varint_at(data, pos)
        coin_len, pos = varint_at(data, pos); pos += coin_len
    return totals


def test_add_champion_creates_full_starter_deck_of_owned_instances():
    """Issue #33: the champion deck holds owned instances and reaches the
    client in AddChampionResponse and in the login EncodedDecks push."""
    import db as db_layer
    from encoded_decks import encode_encoded_decks
    from profile_db import db_card_instance_for_encoded_deck, db_get_decks
    user_id = db_get_or_create_user("StarterDeckPlayer")["id"]
    response = _add_champion(user_id, "Fresh")
    champion_id = response["ChampionBits"]["Id"]
    deck_id = db_champion_last_deck(champion_id)[0]
    owner, deck_name, cards = _deck_row(deck_id)
    assert owner == user_id and deck_name == "Fresh's Coyotle Deck", deck_name
    assert len(cards) == 60 and all(isinstance(c, int) for c in cards), cards
    assert len(set(cards)) == 60, "a deck never holds one instance twice"
    assert _templates_of(user_id, cards) == _expected_templates()

    deck_bits = response["DeckBits"]
    assert deck_bits["Id"] == deck_id and deck_bits["DeckName"] == deck_name
    assert deck_bits["PVEChampionId"] == champion_id
    sent_cards = deck_bits["CardsInDeck"]
    assert [card["Id"] for card in sent_cards] == cards
    assert sorted(card["TemplateID"]["guid"] for card in sent_cards) == \
        _expected_templates()
    shard_rows = db_layer._db.execute(
        "SELECT COUNT(*) FROM card_templates WHERE guid IN (%s) "
        "AND name LIKE '%%Shard%%'" % ",".join("?" * len(_starter_cards())),
        [guid for guid, _ in _starter_cards()]).fetchone()[0]
    assert shard_rows > 0, "the starter list includes its shards"

    assert all(db_card_instance_for_encoded_deck(user_id, c) for c in cards)
    decks = [d for d in db_get_decks(user_id) if d["id"] == deck_id]
    assert _encoded_deck_card_counts(
        encode_encoded_decks(decks, user_id, conn=db_layer._db)) == [60]


def test_second_champion_of_race_reuses_owned_starter_cards():
    import db as db_layer
    user_id = db_get_or_create_user("SecondStarterPlayer")["id"]
    _add_champion(user_id, "First")

    def owned():
        return (db_layer._db.execute(
                    "SELECT COUNT(*) FROM card_instances WHERE user_id=?",
                    (user_id,)).fetchone()[0],
                db_layer._db.execute(
                    "SELECT SUM(quantity) FROM collections WHERE user_id=?",
                    (user_id,)).fetchone()[0])
    before = owned()
    response = _add_champion(user_id, "Second")
    assert owned() == before, (before, owned())
    grants = db_layer._db.execute(
        "SELECT COUNT(*) FROM store_purchases WHERE user_id=? "
        "AND item_name LIKE 'PvE % Starter'", (user_id,)).fetchone()[0]
    assert grants == 1, grants
    _owner, _name, cards = _deck_row(response["DeckBits"]["Id"])
    assert len(set(cards)) == 60 and len(response["DeckBits"]["CardsInDeck"]) == 60
    assert _templates_of(user_id, cards) == _expected_templates()


def test_schema_boot_repairs_template_guid_decks():
    import json
    import db as db_layer
    import static
    user_id = db_get_or_create_user("GuidDeckPlayer")["id"]
    _add_champion(user_id, "Owner")
    guids = [guid for guid, count in _starter_cards() for _ in range(count)]
    kept = db_layer._db.execute(
        "SELECT instance_id FROM card_instances WHERE user_id=? "
        "AND template_guid=? ORDER BY instance_id LIMIT 1",
        (user_id, guids[0])).fetchone()[0]
    legacy = [kept] + guids[1:]
    cur = db_layer._db.execute(
        "INSERT INTO decks (user_id, deck_name, cards, pve_champion_id) "
        "VALUES (?, 'Legacy Deck', ?, 0)", (user_id, json.dumps(legacy)))
    deck_id = cur.lastrowid
    db_layer._db.commit()
    static.ensure_schema(db_layer._db)
    _owner, _name, cards = _deck_row(deck_id)
    assert cards[0] == kept and len(set(cards)) == 60, cards
    assert all(isinstance(c, int) for c in cards), cards
    assert _templates_of(user_id, cards) == sorted(guids)
    from services.starter_deck import repair_template_guid_decks
    assert repair_template_guid_decks(db_layer._db) == 0
    assert _deck_row(deck_id)[2] == cards

if __name__ == "__main__":
    test_inventory_consume_and_upsert_are_connection_scoped()
    test_deck_champion_and_card_instance_helpers_preserve_ownership()
    test_adjust_user_currency_is_atomic_and_connection_scoped()
    test_reset_account_keeps_pve_and_extended_art_cards()
    test_new_user_receives_only_public_command_welcome_mail()
    test_new_player_starts_with_only_the_basic_shards()
    test_add_champion_creates_full_starter_deck_of_owned_instances()
    test_second_champion_of_race_reuses_owned_starter_cards()
    test_schema_boot_repairs_template_guid_decks()
    print("profile DB tests passed")
