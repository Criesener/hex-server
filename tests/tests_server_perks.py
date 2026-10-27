"""Optional economy perks for solo/collection servers (issue #14)."""

import os
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import db
import hconnect_server
import pve_db
from profile_db import (db_create_treasure_chest, db_get_or_create_user,
                        db_get_user_currency)
from services import account_xp, server_perks
from services import wheel_of_fate as wof
from services.chest_loot import SET1
from services.store import apply_redeem

ALL_ON = {"gold_rewards_also_grant_platinum": True,
          "level_up_currency_per_level": 1000,
          "wheel_paid_respin_refund": True}
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


def _perks(settings):
    return mock.patch.object(server_perks, "config", lambda: settings)


def _user(name, gold=0):
    user = db_get_or_create_user(name)["id"]
    db._db.execute("UPDATE users SET gold=?, platinum=0 WHERE id=?", (gold, user))
    db._db.commit()
    return user


def _balance(user):
    return (db_get_user_currency(user, "gold", conn=db._db),
            db_get_user_currency(user, "platinum", conn=db._db))


def _only(outcome):
    """Every spin lands on ``outcome`` with no gold or red reels."""
    table = {name: (1 if name == outcome else 0,) + row[1:]
             for name, row in wof.OUTCOMES.items()}
    return (mock.patch.dict(wof.OUTCOMES, table),
            mock.patch.object(wof, "RED_CHANCE", 0.0),
            mock.patch.object(wof, "GOLD_CHANCE", 0.0))


def _spin(user, chest_db_id, outcome):
    handler = mock.Mock(user_profile={"id": user})
    patches = _only(outcome)
    with patches[0], patches[1], patches[2]:
        return hconnect_server._spin_client_chest(handler, 9000 + chest_db_id)


def test_gold_rewards_bring_the_same_platinum():
    user = _user("perk-gold")
    with _perks(ALL_ON):
        assert server_perks.grant_reward_gold(user, 250, db._db) == (250, 250, 250)
    with _perks({}):
        assert server_perks.grant_reward_gold(user, 100, db._db) == (350, 250, 0)


def test_redeem_code_gold_brings_platinum():
    user = _user("perk-redeem")
    with _perks(ALL_ON):
        result = apply_redeem(db._db, user, "5000gold")
    assert (result["gold"], result["platinum"]) == (5000, 5000), result
    assert _balance(user) == (5000, 5000)


def test_fra_cash_out_brings_platinum():
    user = _user("perk-fra")
    loot = [{"gold": 120}, {"gold": 80}]
    with _perks(ALL_ON), \
            mock.patch.object(pve_db, "db_get_arena_state",
                              return_value={"deck_id": 7, "gold_earned": 2}), \
            mock.patch.object(pve_db, "_fra_gold_rewards", return_value=loot):
        result = pve_db.db_claim_arena_rewards(user, conn=db._db)
    assert (result["gold"], result["platinum"]) == (200, 200), result
    assert _balance(user) == (200, 200)


def test_level_up_pays_level_times_1000_gold_and_platinum():
    user = _user("perk-level")
    account_xp.award(db._db, user, 2800)          # levels 2 and 3
    with _perks(ALL_ON):
        result = account_xp.sync_level(db._db, user, rng=random.Random(1))
    assert (result["gold"], result["platinum"]) == (5000, 5000), result
    assert _balance(user) == (5000, 5000)
    body = db._db.execute(
        "SELECT body FROM emails WHERE user_id=? AND subject="
        "'Account level 3 reached'", (user,)).fetchone()[0]
    assert "3,000 Gold and 3,000 Platinum" in body, body
    with _perks(ALL_ON):
        again = account_xp.sync_level(db._db, user)
    assert again["gold"] == 0 and _balance(user) == (5000, 5000)


def test_paid_respin_refunds_its_cost_at_the_new_rarity():
    user = _user("perk-wheel", gold=5000)
    chest = db_create_treasure_chest(user, SET1, "Common", conn=db._db)
    with _perks(ALL_ON):
        spin = _spin(user, chest, "paid_spin")
        assert spin["gold"] == 1200, spin
        assert _balance(user) == (5000, 0)
        # Hands: upgrade to Uncommon plus a paid re-spin at Uncommon's price.
        spin = _spin(user, chest, "upgrade_paid")
        assert (spin["rarity"], spin["gold"]) == ("Uncommon", 3100), spin
        assert _balance(user) == (5000 - 1200 + 3100, 0)
        # A spin that ends the sequence refunds nothing.
        spin = _spin(user, chest, "fail")
        assert spin["gold"] == 0 and _balance(user) == (5000 - 1200, 0)


def test_perks_off_keep_the_original_economy():
    user = _user("perk-off", gold=5000)
    chest = db_create_treasure_chest(user, SET1, "Common", conn=db._db)
    with _perks({}):
        spin = _spin(user, chest, "paid_spin")
        assert spin["gold"] == 0 and _balance(user) == (3800, 0)
        account_xp.award(db._db, user, 1000)
        result = account_xp.sync_level(db._db, user)
        assert result["gold"] == 0 and _balance(user) == (3800, 0)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            run(name, fn)
    print(f"{len(FAILURES)} failed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
