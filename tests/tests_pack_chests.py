"""Opening booster packs awards one treasure chest per pack."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
# The OpenCardPack log line prints "→", which a Windows console can't encode.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests.test_db import fresh_database

SRC = fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
from profile_db import db_add_inventory, db_get_unopened_chests_full

SET1 = "0382f729-7710-432b-b761-13677982dcd2"
SET1_BOOSTER = "a8b78207-686a-4994-b6cd-4548d1349841"
SET1_PRIMAL_PACK = "8d20082a-4163-4f42-8fce-d4c056f9da04"
FAILURES = []


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        FAILURES.append(name)
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


class _PackHandler(hconnect_server.HCPHandler):
    """Drives the real OpenCardPack handler with the transport replaced."""

    def __init__(self, user_id):
        self.user_profile = {"id": user_id}
        self.client_uid = 1
        self.scnt = 0
        self.sid = "test"
        self.sent = []

    def send(self, headers, body=b""):
        self.sent.append(body)

    def push_inventory_to_client(self, qty=1, template_guid="", item_id=1001):
        pass


def _open_packs(user_id, pack_guid, amount):
    db._db.execute("INSERT INTO users (id, name) VALUES (?,?)",
                   (user_id, f"PackTester{user_id}"))
    db_add_inventory(user_id, pack_guid, amount, conn=db._db)
    db._db.commit()
    handler = _PackHandler(user_id)
    item_id = f"ItemId;1;1;1;m_Guid;2;2;0;36;{pack_guid}".encode()
    handler._handle_service_request_legacy(
        "t", "i", 2127, 2, 0, "00000000-0000-0000-0000-000000000000", 0,
        {"ItemId": "{}", "OpenAmount": amount}, item_id)
    assert handler.sent, "no response sent"
    body = handler.sent[-1]
    listed = re.search(rb"NewChestInstances;\d+;\d+;0;(\d+);", body)
    assert listed, "NewChestInstances missing from the response"
    rows = db_get_unopened_chests_full(user_id, conn=db._db)
    return int(listed.group(1)), [(row[1], row[2]) for row in rows]


def test_each_booster_pack_awards_a_chest():
    listed, chests = _open_packs(9501, SET1_BOOSTER, 5)
    assert listed == 5, listed
    assert len(chests) == 5, chests
    assert all(set_guid == SET1 for set_guid, _ in chests), chests


def test_primal_packs_award_legendary_chests():
    listed, chests = _open_packs(9502, SET1_PRIMAL_PACK, 2)
    assert listed == 2, listed
    assert [rarity for _, rarity in chests] == ["Legendary", "Legendary"], chests


def test_chest_rarities_follow_the_table():
    import random
    rarities = hconnect_server._roll_pack_chest_rarities(4000, rng=random.Random(3))
    assert len(rarities) == 4000
    assert 0.77 < rarities.count("Common") / 4000 < 0.83, rarities.count("Common")
    assert hconnect_server._roll_pack_chest_rarities(3, is_primal=True) == ["Legendary"] * 3


def test_primal_packs_hold_13_rares_and_2_legendaries():
    """Issue #28: Primal Packs opened with the 12/4/1 booster layout."""
    from collections import Counter
    from services.card_pools import generate_booster, load_card_templates
    templates = load_card_templates()
    rarity = {card[0]: card[2] for cards in templates.values() for card in cards}
    primal = generate_booster(templates, SET1, "PrimalPack")
    counts = Counter(rarity[guid] for guid, *_ in primal)
    assert counts == {"Rare": 13, "Legendary": 2}, counts
    assert len({guid for guid, *_ in primal}) == 15, primal
    booster = Counter(rarity[guid] for guid, *_ in generate_booster(templates, SET1))
    assert booster["Common"] == 12 and booster["Uncommon"] == 4, booster
    assert booster["Rare"] + booster["Legendary"] == 1, booster


TRANQUIL_DREAM_PACK = "88d29ef0-5ec7-49b1-bcc5-8f70c5c4cda5"
AZ1_PACK_TABLE_GUIDS = (
    "7b8390fd-7d3d-44d4-b285-1aeae3aef98b",   # Howling Plains (AZ1) Pack
    TRANQUIL_DREAM_PACK,
    "faea143f-7193-4ac1-bbb2-f79469d8ad9f",   # Fort Romor Pack
    "eda1cb9f-9900-4218-a8d1-6564573a9cf2",   # The Usurper Pack
    "56e231a3-f752-4e87-b04a-51c508c87c2d",   # Smoldering Dead Pack
    "1d54da38-e03e-449c-aa2d-c9a7c0ad2cb1",   # Devonshire Keep Pack
)


def _promo_chest(template_guid):
    return hconnect_server.ClientChest(0, template_guid, "", "Promo", None, None)


def test_observed_pack_tables_resolve_to_templates():
    """Issue #37: every name of the vendored loot table is a client template."""
    from az1_pack import load_pack_loot_tables
    tables = load_pack_loot_tables()
    assert sorted(tables) == sorted(AZ1_PACK_TABLE_GUIDS), sorted(tables)
    cards = {row[0]: row[1:] for row in db._db.execute(
        "SELECT guid, name, is_pve FROM card_templates")}
    equipment = {row[0]: row[1] for row in db._db.execute(
        "SELECT guid, name FROM equipment_templates")}
    chests = {row[0] for row in db._db.execute("SELECT guid FROM chest_templates")}
    for guid, table in tables.items():
        assert guid in chests, guid
        assert table.pve_cards and table.equipment, table.name
        for entry in table.pve_cards:
            assert entry.guid in cards, (table.name, entry)
            name, is_pve = cards[entry.guid]
            assert is_pve and name.lower() == entry.name.lower(), (entry, name)
        for entry in table.equipment:
            assert equipment.get(entry.guid, "").lower() == entry.name.lower(), \
                (table.name, entry)


def _split_pack(cards, items, templates):
    by_guid = {card[0]: card for cards_ in templates.values() for card in cards_}
    pvp = [by_guid[guid] for guid, *_ in cards if not by_guid[guid][6]]
    pve = [by_guid[guid] for guid, *_ in cards if by_guid[guid][6]]
    return pvp, pve, [kind for _, kind in items]


def test_observed_packs_open_with_the_az1_recipe():
    """Each AZ1 pack: 2 PvP commons, 1 PvE card, 2 equipment/Stardust slots."""
    import random
    from az1_pack import load_pack_loot_tables
    templates = hconnect_server._load_card_templates()
    tables = load_pack_loot_tables()
    for guid in AZ1_PACK_TABLE_GUIDS:
        table_cards = {entry.guid for entry in tables[guid].pve_cards}
        table_gear = {entry.guid for entry in tables[guid].equipment}
        for seed in range(20):
            cards, items = hconnect_server._generate_chest_rewards(
                _promo_chest(guid), templates, random.Random(seed))
            pvp, pve, kinds = _split_pack(cards, items, templates)
            assert len(pvp) == 2 and all(card[2] == "Common" for card in pvp), pvp
            assert len(pve) == 1 and pve[0][0] in table_cards, (guid, pve)
            assert len(kinds) == 2 and set(kinds) <= {"equipment", "stardust"}, kinds
            assert all(item in table_gear for item, kind in items
                       if kind == "equipment"), (guid, items)


def test_tranquil_dream_pve_cards_follow_the_observed_weights():
    import random
    from collections import Counter
    from az1_pack import STARDUST_REPLACEMENT_RATE, generate_observed_pack, \
        pack_loot_table
    templates = hconnect_server._load_card_templates()
    table = pack_loot_table(TRANQUIL_DREAM_PACK)
    names = {entry.guid: entry.name for entry in table.pve_cards}
    assert sorted(names.values()) == [
        "Dream Bear", "Dream Eagle", "Dream Skarn", "Dream Spider",
        "Dream Turtle", "Enter the Dream"], names
    rng = random.Random(37)
    rolls = 6000
    pve, gear, dust = Counter(), Counter(), 0
    for _ in range(rolls):
        reward = generate_observed_pack(templates, table, rng)
        assert reward.cards[2][0] in names, reward.cards
        pve[reward.cards[2][0]] += 1
        gear.update(reward.equipment_guids)
        dust += len(reward.stardust_rarities)
    total = sum(entry.weight for entry in table.pve_cards)
    for entry in table.pve_cards:
        share = pve[entry.guid] / rolls
        assert abs(share - entry.weight / total) < 0.025, (entry.name, share)
    assert abs(dust / (2 * rolls) - STARDUST_REPLACEMENT_RATE) < 0.02, dust
    gear_total = sum(entry.weight for entry in table.equipment)
    bulwark = next(e for e in table.equipment if e.name == "Turtle Shell Bulwark")
    assert bulwark.unique
    expected = 2 * rolls * (1 - STARDUST_REPLACEMENT_RATE) * bulwark.weight / gear_total
    assert abs(gear[bulwark.guid] - expected) < 0.2 * expected, \
        (gear[bulwark.guid], expected)


def test_unknown_promo_chest_warns_instead_of_opening_empty_silently():
    templates = hconnect_server._load_card_templates()
    logged = []
    original = hconnect_server.log
    hconnect_server.log = lambda msg, *args, **kwargs: logged.append(msg)
    try:
        cards, items = hconnect_server._generate_chest_rewards(
            _promo_chest("00000000-0000-0000-0000-0000000c0037"), templates)
    finally:
        hconnect_server.log = original
    assert cards == [] and items == [], (cards, items)
    assert any("no loot source" in msg and "0000000c0037" in msg
               for msg in logged), logged


if __name__ == "__main__":
    run("each booster pack awards a chest", test_each_booster_pack_awards_a_chest)
    run("Primal Packs award Legendary chests", test_primal_packs_award_legendary_chests)
    run("chest rarities follow the table", test_chest_rarities_follow_the_table)
    run("Primal Packs hold 13 Rares and 2 Legendaries",
        test_primal_packs_hold_13_rares_and_2_legendaries)
    run("observed pack tables resolve to templates",
        test_observed_pack_tables_resolve_to_templates)
    run("observed AZ1 packs open with the AZ1 recipe",
        test_observed_packs_open_with_the_az1_recipe)
    run("Tranquil Dream PvE cards follow the observed weights",
        test_tranquil_dream_pve_cards_follow_the_observed_weights)
    run("unknown promo chest warns",
        test_unknown_promo_chest_warns_instead_of_opening_empty_silently)
    if FAILURES:
        sys.exit(1)
