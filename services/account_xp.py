"""Account XP and levels (issue #4).

Campaign XP, Frost Ring Arena tiers and the daily login add account XP.  The
level curve lives in ``account_levels.json``; every level-up grants one random
deck sleeve the player does not own yet.  The client reads the level and XP
bar from ``reckoning_bits`` plus an ``AccountXpCap`` object (login profile
stream, or a live ``ProfileGenericAccountUpdate``); without that object its
account window threw a NullReferenceException and showed level 0.
"""

import json
import os
import random
import time
from functools import lru_cache

from db import log_req

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "account_levels.json")
# The client subtracts the XP gained this period from these; there is no
# daily cap or bonus period on this server, so keep the cap out of reach.
NO_CAP = 999999
TIMER_HOURS = 24


@lru_cache(maxsize=None)
def config():
    with open(_PATH) as f:
        return json.load(f)


def level_threshold(level):
    """Total XP needed to reach ``level`` (level 1 needs 0)."""
    thresholds = config()["thresholds"]
    if level <= len(thresholds):
        return int(thresholds[max(level, 1) - 1])
    return int(thresholds[-1]) + (level - len(thresholds)) * int(
        config()["step_after_table"])


def level_for_xp(xp):
    level = 1
    while level_threshold(level + 1) <= int(xp or 0):
        level += 1
    return level


def award(conn, user_id, xp):
    """Add account XP in the caller's transaction (levels sync separately)."""
    xp = int(xp or 0)
    if user_id and xp > 0:
        conn.execute("UPDATE users SET experience=COALESCE(experience,0)+? "
                     "WHERE id=?", (xp, user_id))
    return xp


@lru_cache(maxsize=None)
def _sleeve_pool():
    from gamedata import DEFAULT_RECORD_STORE
    excluded = {g.lower() for g in config().get("excluded_sleeves", ())}
    return tuple(sorted(
        (record.guid, str(record.field("m_Name") or ""))
        for record in DEFAULT_RECORD_STORE.load("InventoryItemData")
        if record.is_a("InventoryDeckSleeve")
        and record.guid.lower() not in excluded))


def _owned_inventory(conn, user_id):
    return {str(row[0]).lower() for row in conn.execute(
        "SELECT template_guid FROM player_inventory "
        "WHERE user_id=? AND quantity>0", (user_id,)).fetchall()}


def missing_sleeves(conn, user_id):
    owned = _owned_inventory(conn, user_id)
    return [(guid, name) for guid, name in _sleeve_pool()
            if guid.lower() not in owned]


def sync_level(conn, user_id, rng=random):
    """Raise the stored level to the XP's level, granting a sleeve per level.

    Returns ``{"xp", "level", "old_level", "sleeves", "inventory_updates"}``
    where ``inventory_updates`` are ``(template_guid, client_uid, quantity)``
    rows for InventoryUpdated pushes.  The caller commits.
    """
    from profile_db import (db_next_inventory_client_uid, db_send_email,
                            db_upsert_inventory_item)
    row = conn.execute("SELECT COALESCE(experience,0), COALESCE(level,1) "
                       "FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    xp, old_level = int(row[0]), max(1, int(row[1]))
    level = level_for_xp(xp)
    result = {"xp": xp, "level": max(level, old_level), "old_level": old_level,
              "sleeves": [], "inventory_updates": []}
    if level <= old_level:
        return result
    missing = missing_sleeves(conn, user_id)
    next_uid = db_next_inventory_client_uid(user_id, conn=conn)
    for reached in range(old_level + 1, level + 1):
        if missing:
            guid, name = missing.pop(rng.randrange(len(missing)))
            client_uid = db_upsert_inventory_item(
                user_id, guid, 1, client_item_uid=next_uid, conn=conn)
            if client_uid == next_uid:
                next_uid += 1
            result["sleeves"].append((guid, name))
            result["inventory_updates"].append((guid, client_uid, 1))
            reward = f"the deck sleeve '{name}'"
        else:
            reward = "no new sleeve (you own them all)"
        db_send_email(
            user_id, f"Account level {reached} reached",
            f"Congratulations, your account reached level {reached}! "
            f"Your reward: {reward}.\n\nThank you for playing Hex: Shards of Fate!",
            "SYSTEM", conn=conn)
    conn.execute("UPDATE users SET level=? WHERE id=?", (level, user_id))
    log_req(f"    Account level {old_level} -> {level} (xp={xp}), "
            f"sleeves={[name for _guid, name in result['sleeves']]}")
    return result


def cap_info(level):
    return {"BonusMod": 1.0, "BonusCap": 0, "XpCap": NO_CAP,
            "TimerDurHours": TIMER_HOURS,
            "XpForCurrentLevel": level_threshold(level),
            "XpForNextLevel": level_threshold(level + 1),
            "HasUnClaimed": False}


def _write_cap_fields(b, level):
    info = cap_info(level)
    b.field_float("BonusMod", info["BonusMod"])
    b.field_int("BonusCap", info["BonusCap"])
    b.field_int("XpCap", info["XpCap"])
    b.field_int("TimerDurHours", info["TimerDurHours"])
    b.field_int("XpForCurrentLevel", info["XpForCurrentLevel"])
    b.field_int("XpForNextLevel", info["XpForNextLevel"])
    b.field_bool("HasUnClaimed", info["HasUnClaimed"])


def encode_cap_info(level):
    """Standalone AccountXpCap object for the login profile stream."""
    from objfmt_builder import ObjFmtBuilder
    b = ObjFmtBuilder("Game.Shared.Profile.Network+AccountXpCap")
    _write_cap_fields(b, level)
    return b.finish(7)


def _begin_struct(b, name, type_name, num_props):
    index = b.begin_element(name, type_name, num_props)
    return index, b._element_starts[index]


def encode_account_update(xp, level):
    """ProfileGenericAccountUpdate carrying the account XP/level and cap."""
    from objfmt_builder import ObjFmtBuilder
    now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    b = ObjFmtBuilder("Game.Shared.ProfileGenericAccountUpdate")
    index, start = _begin_struct(b, "Account",
                                 "Game.Shared.Domain.reckoning_bits", 4)
    b.field_int("ExperiencePoints", int(xp))
    b.field_int("Level", int(level))
    b.field_datetime("XpGainTimer", now)
    b.field_int("XpGain", 0)
    b._set_size(index, start)
    index, start = _begin_struct(b, "CapInfo",
                                 "Game.Shared.Profile.Network+AccountXpCap", 7)
    _write_cap_fields(b, level)
    b._set_size(index, start)
    return b.finish(2)


def state_response(conn, user_id, level):
    """``acctxpget`` envelope: the next level offers a random missing sleeve."""
    missing = missing_sleeves(conn, user_id)
    rewards = [{"Level": level + 1, "Platform": "", "RewardID": f"acct-{level + 1}",
                "PayoutID": "acct-sleeves", "Boa": True, "Quantity": 1,
                "RewardType": "SLVSET", "LinkReward": "", "ChanceOfReward": 1.0,
                "ExpireSec": 0}]
    # The client counts the SLEEVES rows of a set to word "one of N sleeves".
    # Their level must be outside the earned/next-level ranges.
    rewards += [{"Level": 1000000, "Platform": "", "RewardID": "acct-sleeves",
                 "PayoutID": guid, "Boa": True, "Quantity": 1,
                 "RewardType": "SLEEVES", "LinkReward": "",
                 "ChanceOfReward": 1.0, "ExpireSec": 0}
                for guid, _name in missing]
    return json.dumps({"State": [], "Rewards": rewards},
                      separators=(",", ":")).encode("utf-8")
