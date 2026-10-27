"""Regression tests for account XP, levels and level-up sleeves (issue #4)."""

import json
import os
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import hconnect_server
from application.objfmt_wire import parse_datawrapper
from profile_db import db_get_or_create_user
from services import account_xp

_db = hconnect_server._db


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


def test_level_curve_uses_the_champion_table_then_fixed_steps():
    assert account_xp.level_for_xp(0) == 1
    assert account_xp.level_for_xp(999) == 1
    assert account_xp.level_for_xp(1000) == 2
    assert account_xp.level_for_xp(110000) == 15
    assert account_xp.level_for_xp(129999) == 15
    assert account_xp.level_for_xp(130000) == 16


def test_level_up_grants_one_missing_sleeve_per_level_once():
    user = db_get_or_create_user("AccountXpTester")["id"]
    missing_before = len(account_xp.missing_sleeves(_db, user))
    assert missing_before > 100, missing_before
    account_xp.award(_db, user, 2800)
    result = account_xp.sync_level(_db, user, rng=random.Random(1))
    assert (result["old_level"], result["level"]) == (1, 3), result
    guids = [guid for guid, _name in result["sleeves"]]
    assert len(guids) == 2 and len(set(guids)) == 2, guids
    assert len(account_xp.missing_sleeves(_db, user)) == missing_before - 2
    mails = _db.execute(
        "SELECT COUNT(*) FROM emails WHERE user_id=? AND subject LIKE "
        "'Account level %'", (user,)).fetchone()[0]
    assert mails == 2, mails
    again = account_xp.sync_level(_db, user)
    assert again["level"] == 3 and not again["sleeves"], again


def test_cap_info_and_account_update_decode():
    cap = parse_datawrapper(account_xp.encode_cap_info(3))
    assert cap["XpForCurrentLevel"] == 2800, cap
    assert cap["XpForNextLevel"] == 5000, cap
    assert cap["HasUnClaimed"] is False, cap
    update = parse_datawrapper(account_xp.encode_account_update(3100, 3),
                               preserve_complex=True)
    assert update["Account"]["ExperiencePoints"] == 3100, update
    assert update["Account"]["Level"] == 3, update
    assert update["CapInfo"]["XpForNextLevel"] == 5000, update


def test_state_response_offers_a_random_missing_sleeve_next():
    user = db_get_or_create_user("AccountXpStateTester")["id"]
    body = json.loads(account_xp.state_response(_db, user, 4))
    next_level = [r for r in body["Rewards"] if r["Level"] == 5]
    assert [r["RewardType"] for r in next_level] == ["SLVSET"], next_level
    pool = [r for r in body["Rewards"] if r["RewardType"] == "SLEEVES"]
    assert len(pool) == len(account_xp.missing_sleeves(_db, user))
    assert all(r["RewardID"] == next_level[0]["PayoutID"] for r in pool)


def test_campaign_reward_marks_account_xp_for_push():
    import campaign
    user = db_get_or_create_user("AccountXpCampaignTester")["id"]
    pushed = []
    handler = SimpleNamespace(push_account_xp=lambda: pushed.append(True),
                              _log_req=lambda *_a: None)
    before = _db.execute("SELECT experience FROM users WHERE id=?",
                         (user,)).fetchone()[0] or 0
    campaign._award_account_xp(handler, _db, user, 200)
    after = _db.execute("SELECT experience FROM users WHERE id=?",
                        (user,)).fetchone()[0]
    assert after - before == 200
    campaign.push_pending_account_xp(handler)
    campaign.push_pending_account_xp(handler)
    assert pushed == [True], pushed


if __name__ == "__main__":
    run("level curve", test_level_curve_uses_the_champion_table_then_fixed_steps)
    run("level-up grants one missing sleeve per level",
        test_level_up_grants_one_missing_sleeve_per_level_once)
    run("cap info and account update decode",
        test_cap_info_and_account_update_decode)
    run("state response offers a random missing sleeve",
        test_state_response_offers_a_random_missing_sleeve_next)
    run("campaign reward marks account XP for push",
        test_campaign_reward_marks_account_xp_for_push)
