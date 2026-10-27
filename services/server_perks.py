"""Optional economy perks for solo/collection servers (issue #14).

The switches live in ``server_perks.json``; with every perk off the server
keeps the original economy.
"""

import json
import os
from functools import lru_cache

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "server_perks.json")


@lru_cache(maxsize=None)
def config():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def reward_platinum(gold):
    """Platinum that comes with a gold reward of ``gold``."""
    if not config().get("gold_rewards_also_grant_platinum"):
        return 0
    return max(0, int(gold or 0))


def grant_reward_gold(user_id, gold, conn):
    """Credit a gold reward plus its perk platinum in the caller's transaction.

    Returns ``(new_gold, new_platinum, platinum_granted)``.
    """
    from profile_db import db_adjust_user_currency
    platinum = reward_platinum(gold)
    new_gold, new_platinum = db_adjust_user_currency(
        user_id, gold_delta=gold, platinum_delta=platinum, conn=conn)
    return new_gold, new_platinum, platinum


def level_up_currency(level):
    """Gold (and the same platinum) for reaching account level ``level``."""
    per_level = int(config().get("level_up_currency_per_level", 0) or 0)
    return max(0, per_level) * int(level)


def paid_respin_refund(spin_status, rarity):
    """Gold that pays for the paid re-spin a spin left the chest with."""
    from services import wheel_of_fate as wof
    if spin_status != wof.PAID_SPIN or not config().get("wheel_paid_respin_refund"):
        return 0
    return wof.spin_cost(rarity, spin_status)
