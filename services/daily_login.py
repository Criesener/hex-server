"""Collection effects that fire on a player's first login of the day."""

from datetime import datetime, timezone

from db import log_req

# Spectral Oak: "The first time that you login each day, this creates a
# Spectral Acorn and adds it to your collection."
SPECTRAL_OAK = "79ba006b-5fae-44f2-b5f5-024c5d73a3b0"
SPECTRAL_ACORN = "0cfb7ee4-bb48-4606-bd27-fa6e13ad21c4"
DAILY_CARD_GRANTS = ((SPECTRAL_OAK, SPECTRAL_ACORN),)


def grant_daily_login_cards(conn, user_id, today=None):
    """Grant each owned daily-login source its card once per UTC day.

    Returns the number of card copies added.  The caller commits.
    """
    from profile_db import (db_add_collection, db_insert_card_instance,
                            db_next_card_instance_for_user)
    today = today or datetime.now(timezone.utc).date().isoformat()
    added = 0
    for source_guid, card_guid in DAILY_CARD_GRANTS:
        owned = conn.execute(
            "SELECT COUNT(*) FROM card_instances WHERE user_id=? "
            "AND lower(template_guid)=?", (user_id, source_guid)).fetchone()[0]
        if not owned:
            continue
        row = conn.execute(
            "SELECT last_date FROM daily_login_grants WHERE user_id=? "
            "AND grant_key=?", (user_id, source_guid)).fetchone()
        if row and row[0] == today:
            continue
        conn.execute(
            "INSERT INTO daily_login_grants (user_id, grant_key, last_date) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, grant_key) "
            "DO UPDATE SET last_date=excluded.last_date",
            (user_id, source_guid, today))
        next_id = db_next_card_instance_for_user(user_id, conn=conn)
        db_add_collection(user_id, card_guid, owned, conn=conn)
        for offset in range(owned):
            db_insert_card_instance(user_id, next_id + offset, card_guid,
                                    conn=conn)
        added += owned
        log_req(f"    Daily login: {owned}x {card_guid} for {source_guid}")
    return added
