"""Per-champion race starter decks.

Every new champion gets its race's starter deck (``generated/starter_decks.json``,
shards included).  Deck rows store card *instance* IDs, the same format as
every other deck, so the login ``EncodedDecks`` push and the client's deck
builder can resolve them.

The helpers only use the connection they are given and plain SQL, so the
caller owns the transaction (``db.transaction()`` for AddChampion, the schema
connection for the boot repair).
"""

import json
import re

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FIRST_INSTANCE_ID = 5001


def is_template_guid(entry):
    """Whether a deck entry is a card template GUID instead of an instance ID."""
    return isinstance(entry, str) and bool(_GUID_RE.match(entry))


def _next_instance_id(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(instance_id), ?) + 1 FROM card_instances "
        "WHERE user_id=?", (_FIRST_INSTANCE_ID - 1, user_id)).fetchone()
    return int(row[0]) if row else _FIRST_INSTANCE_ID


def _add_collection(conn, user_id, template_guid, quantity):
    row = conn.execute(
        "SELECT id FROM collections WHERE user_id=? AND card_template_id=?",
        (user_id, template_guid)).fetchone()
    if row:
        conn.execute("UPDATE collections SET quantity=quantity+? WHERE id=?",
                     (int(quantity), row[0]))
    else:
        conn.execute(
            "INSERT INTO collections (user_id, card_template_id, quantity) "
            "VALUES (?, ?, ?)", (user_id, template_guid, int(quantity)))


def allocate_deck_instances(conn, user_id, cards, reuse_owned=True,
                            exclude=()):
    """Return one instance ID per deck copy of ``cards``.

    ``cards`` is ``[(template_guid, count), ...]``.  With ``reuse_owned`` the
    account's existing instances of each template are used first (never the
    same instance twice in one deck, nor any ID in ``exclude``); only the
    missing copies are created and added to the collection.  Without it every
    copy is a new instance (a first grant).

    Returns ``(instance_ids, created)`` with ``instance_ids`` in deck order.
    """
    used = {int(entry) for entry in exclude}
    next_id = _next_instance_id(conn, user_id)
    instance_ids = []
    created = 0
    for template_guid, count in cards:
        count = int(count)
        picked = []
        if reuse_owned and count > 0:
            for row in conn.execute(
                    "SELECT instance_id FROM card_instances "
                    "WHERE user_id=? AND template_guid=? ORDER BY instance_id",
                    (user_id, template_guid)).fetchall():
                if len(picked) >= count:
                    break
                instance_id = int(row[0])
                if instance_id not in used:
                    picked.append(instance_id)
                    used.add(instance_id)
        missing = count - len(picked)
        for _ in range(missing):
            conn.execute(
                "INSERT INTO card_instances (user_id, instance_id, template_guid) "
                "VALUES (?, ?, ?)", (user_id, next_id, template_guid))
            picked.append(next_id)
            used.add(next_id)
            next_id += 1
        if missing:
            _add_collection(conn, user_id, template_guid, missing)
            created += missing
        instance_ids.extend(picked)
    return instance_ids, created


def starter_deck_name(champion_name, race_name):
    return f"{champion_name}'s {race_name} Deck"


def create_champion_starter_deck(conn, user_id, champion_id, champion_name,
                                 race_name, cards, grant_guid=""):
    """Grant (once per race) and build a new champion's starter deck.

    The first champion of a race receives fresh instances of every starter
    card and a ``store_purchases`` marker (``grant_guid``).  Later champions of
    the same race reuse the account's instances and only create copies that
    are missing.  The deck is linked as the champion's last deck.
    """
    first_grant = not (grant_guid and conn.execute(
        "SELECT 1 FROM store_purchases WHERE user_id=? AND item_template_id=?",
        (user_id, grant_guid)).fetchone())
    instance_ids, created = allocate_deck_instances(
        conn, user_id, cards, reuse_owned=not first_grant)
    if first_grant and grant_guid:
        conn.execute(
            "INSERT INTO store_purchases "
            "(user_id, item_name, item_template_id, price, currency) "
            "VALUES (?, ?, ?, 0, 'Free')",
            (user_id, f"PvE {race_name} Starter", grant_guid))
    deck_name = starter_deck_name(champion_name, race_name)
    cursor = conn.execute(
        "INSERT INTO decks (user_id, deck_name, cards, pve_champion_id) "
        "VALUES (?, ?, ?, ?)",
        (user_id, deck_name, json.dumps(instance_ids), champion_id))
    deck_id = int(cursor.lastrowid)
    conn.execute("UPDATE champions SET last_deck_id=? WHERE id=? AND user_id=?",
                 (deck_id, champion_id, user_id))
    return {"deck_id": deck_id, "deck_name": deck_name,
            "instance_ids": instance_ids, "first_grant": first_grant,
            "created": created}


def deck_card_entries(conn, user_id, instance_ids):
    """``(template_guid, name, instance_id, cost, atk, def)`` per owned copy.

    This is the entry shape ``encoder.encode_deck_bits_fields`` encodes as a
    ``card_instance_bits`` in ``CardsInDeck``; unresolved IDs are skipped.
    """
    entries = []
    for instance_id in instance_ids:
        row = conn.execute(
            "SELECT template_guid FROM card_instances "
            "WHERE user_id=? AND instance_id=?",
            (user_id, int(instance_id))).fetchone()
        if row:
            entries.append((row[0], "", int(instance_id), 0, 0, 0))
    return entries


def repair_template_guid_decks(conn):
    """Convert deck entries stored as template GUIDs into owned instance IDs.

    Older champion starter decks stored template GUIDs, which the login deck
    push cannot resolve.  Each GUID entry is mapped to an owned instance of
    that template that the deck does not already hold; missing copies are
    created.  Idempotent: repaired decks no longer contain GUID entries.
    Returns the number of repaired decks.
    """
    repaired = 0
    rows = conn.execute(
        "SELECT id, user_id, cards FROM decks WHERE cards LIKE '%-%'").fetchall()
    for deck_id, user_id, cards_json in rows:
        try:
            entries = json.loads(cards_json or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list) or not any(
                is_template_guid(entry) for entry in entries):
            continue
        counts = {}
        for entry in entries:
            if is_template_guid(entry):
                counts[entry] = counts.get(entry, 0) + 1
        kept = [int(entry) for entry in entries
                if isinstance(entry, int) and not isinstance(entry, bool)]
        instance_ids, _created = allocate_deck_instances(
            conn, user_id, list(counts.items()), reuse_owned=True,
            exclude=kept)
        by_template = {}
        position = 0
        for template_guid, count in counts.items():
            by_template[template_guid] = instance_ids[position:position + count]
            position += count
        converted = []
        for entry in entries:
            if is_template_guid(entry):
                converted.append(by_template[entry].pop(0))
            else:
                converted.append(entry)
        conn.execute("UPDATE decks SET cards=? WHERE id=?",
                     (json.dumps(converted), deck_id))
        repaired += 1
    return repaired
