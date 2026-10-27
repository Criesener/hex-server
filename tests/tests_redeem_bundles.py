"""Regression tests for redeem_bundles.json codes (Kickstarter tiers) and the
Spectral Oak daily-login Acorn."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import hconnect_server
from profile_db import db_get_or_create_user
from services import redeem_bundles
from services.daily_login import (SPECTRAL_ACORN, SPECTRAL_OAK,
                                  grant_daily_login_cards)
from services.store import apply_redeem

_db = hconnect_server._db
LADY_CASSANDRA = "abd634cb-d56e-43f5-9344-5cefbbcb19e8"
SET1_BOOSTER = "a8b78207-686a-4994-b6cd-4548d1349841"


def run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        print(f"FAIL {name}: {e}")
    except Exception as e:
        import traceback
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _user(name):
    return db_get_or_create_user(name)["id"]


def _copies(user_id, guid):
    return _db.execute(
        "SELECT COUNT(*) FROM card_instances WHERE user_id=? AND template_guid=?",
        (user_id, guid)).fetchone()[0]


def _inventory(user_id, guid):
    row = _db.execute(
        "SELECT quantity FROM player_inventory WHERE user_id=? AND template_guid=?",
        (user_id, guid)).fetchone()
    return row[0] if row else 0


def test_king_merges_lower_tiers_once():
    king = redeem_bundles.resolve("ks-king")
    assert king["boosters"] == {"Shards of Fate": 150}, king["boosters"]
    assert king["starter_decks"] == ["Human", "Orc", "Dwarf", "Elf"]
    assert king["cards"][LADY_CASSANDRA]["qty"] == 1
    assert {"Zoltog", "Monkey of the Nine Tails", "Warrior Sleeve",
            "King Sleeve", "Million Dollar Deck Sleeve"} <= set(king["items"])
    grand = redeem_bundles.resolve("KS-GRANDKING")
    assert grand["cards"][LADY_CASSANDRA]["qty"] == 2
    assert grand["random_equipment"]["count"] == 20
    assert grand["gift_codes"] == ["KS-GUILDGIFT"]


def test_every_bundle_name_resolves():
    from services.redeem_bundles import _inventory_item_guid, _set_guid
    missing = []
    for code in redeem_bundles.load_bundles():
        bundle = redeem_bundles.resolve(code)
        missing += [n for n in bundle["items"] if not _inventory_item_guid(n)]
        missing += [s for s in bundle["boosters"] if not _set_guid(s)]
        for guid in bundle["cards"]:
            if not _db.execute("SELECT 1 FROM card_templates WHERE guid=?",
                               (guid,)).fetchone():
                missing.append(guid)
    assert not missing, sorted(set(missing))


def test_redeem_warrior_grants_once_per_player():
    user = _user("ks-warrior-tester")
    before_packs = _inventory(user, SET1_BOOSTER)
    before_decks = _db.execute(
        "SELECT COUNT(*) FROM decks WHERE user_id=?", (user,)).fetchone()[0]
    result = apply_redeem(_db, user, "ks-warrior")
    assert result["redeemed"], result
    assert _inventory(user, SET1_BOOSTER) - before_packs == 25
    assert _copies(user, LADY_CASSANDRA) == 1
    zoltog = redeem_bundles._inventory_item_guid("Zoltog")
    assert _inventory(user, zoltog) == 1
    decks = _db.execute(
        "SELECT COUNT(*) FROM decks WHERE user_id=?", (user,)).fetchone()[0]
    assert decks - before_decks == 3, (before_decks, decks)
    # The client strips "-" from typed codes; a dash still counts as the same code.
    again = apply_redeem(_db, user, "KSWARRIOR")
    assert not again["redeemed"] and "already" in again["error_message"]
    assert _inventory(user, SET1_BOOSTER) - before_packs == 25
    other = _user("ks-warrior-friend")
    assert apply_redeem(_db, other, "KS-WARRIOR")["redeemed"]


def test_plain_codes_still_work():
    user = _user("plain-code-tester")
    assert apply_redeem(_db, user, "5000gold")["gold"] == 5000
    assert not apply_redeem(_db, user, "no-such-code")["redeemed"]


def test_spectral_oak_grants_one_acorn_per_day():
    user = _user("oak-tester")
    assert grant_daily_login_cards(_db, user, today="2026-10-27") == 0
    hconnect_server.db_create_card_instance(user, 990001, SPECTRAL_OAK, conn=_db)
    assert grant_daily_login_cards(_db, user, today="2026-10-27") == 1
    assert grant_daily_login_cards(_db, user, today="2026-10-27") == 0
    assert grant_daily_login_cards(_db, user, today="2026-10-28") == 1
    assert _copies(user, SPECTRAL_ACORN) == 2



def test_redeem_response_matches_the_legacy_encoding():
    """With no reward ids the builder output decodes like the old encoding."""
    from encoder import encode_objfmt_response
    from services.store import encode_redeem_response
    legacy = encode_objfmt_response(
        ["Game.Client.Network.Escrow.RedeemCodeResponse",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Game.Shared.ResourceId", "System.Guid", "System.Int32",
         "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits",
         "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits",
         "Game.Shared.Network.Escrow.ERedeemCodeError", "System.String"],
        [("ItemTemplateIds", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.ResourceId", 0)),
         ("GoldDelta", "int", 5000), ("PlatinumDelta", "int", 0),
         ("CardBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits", 0)),
         ("StarterDecksBits", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits", 0)),
         ("Error", "enum1", ("Game.Shared.Network.Escrow.ERedeemCodeError", 0)),
         ("ErrorMessage", "string", "")])
    from application.objfmt_wire import parse_datawrapper
    built = encode_redeem_response((), 5000, 0, 0, "")
    parsed, expected = parse_datawrapper(built), parse_datawrapper(legacy)
    assert parsed == expected, (parsed, expected)
    with_ids = encode_redeem_response([SET1_BOOSTER] * 2, 0, 0, 0, "")
    assert with_ids.count(SET1_BOOSTER.encode()) == 2

if __name__ == "__main__":
    run("king merges lower tiers once", test_king_merges_lower_tiers_once)
    run("every bundle name resolves", test_every_bundle_name_resolves)
    run("warrior grants once per player", test_redeem_warrior_grants_once_per_player)
    run("plain codes still work", test_plain_codes_still_work)
    run("spectral oak daily acorn", test_spectral_oak_grants_one_acorn_per_day)
    run("redeem response encoding", test_redeem_response_matches_the_legacy_encoding)
