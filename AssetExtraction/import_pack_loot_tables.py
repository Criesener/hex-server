"""Import the observed AZ1 pack loot tables into ``pack_loot_tables.json``.

Source: ``data/az1_pack_loot_tables.csv`` of the community project
https://github.com/Theveninm/Hex-shard-of-fates-revival (no license stated;
the file holds drop rates counted from opened packs).  The CSV is read as
plain data with the standard library only.

Layout: row 0 holds one header per pack every 9 columns ("<Pack>",
"Number Opened: N"); row 1 holds three column groups per pack (Card Drops,
Equipment Drops, Unique Equips; each name, rarity, "% Drop Chance"); every
later row is one entry per group.

Names are resolved case-insensitively against the bootstrapped database's
``card_templates`` / ``equipment_templates`` (``NAME_FIXES`` handles spelling
the CSV gets wrong).  A card name has several templates (the AZ1 PvE set
``c363c22e`` and the equipment-modified variant set); the canonical PvE
template is picked by ``CARD_SET_PREFERENCE``.  Packs map to chest templates
by name (``PACK_NAME_ALIASES`` for the CSV's own pack names).

Run after a database bootstrap (the extractor only reads the database):
    python3 AssetExtraction/import_pack_loot_tables.py \\
        --csv az1_pack_loot_tables.csv --db "$HEX_DB_PATH"
It exits non-zero when a name does not resolve.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "pack_loot_tables.json")

SOURCE = ("Observed drop rates from data/az1_pack_loot_tables.csv of "
          "https://github.com/Theveninm/Hex-shard-of-fates-revival "
          "(community data, no license stated). Weights are the observed "
          "'% Drop Chance' values; the server normalises them per list.")

# The CSV names the Howling Plains Pack after its adventure zone.
PACK_NAME_ALIASES = {"adventure zone 1 pack": "howling plains pack"}

# Spelling fixes, lower-cased CSV name -> client template name.  Every name
# of the current CSV resolves case-insensitively, so this is empty.
NAME_FIXES = {}

# A card name has a template in the AZ1 PvE set and several in the
# equipment-variant set; the PvE pools use the AZ1 PvE set.
AZ1_PVE_SET = "c363c22e-1c03-43c0-a5d3-e3e8759120e7"
CARD_SET_PREFERENCE = (AZ1_PVE_SET,)
EQUIPMENT_SET_PREFERENCE = (AZ1_PVE_SET,)

GROUPS = (("pve_cards", 0), ("equipment", 3), ("unique_equipment", 6))


def _key(name):
    name = re.sub(r"\s+", " ", str(name or "").replace("’", "'")).strip()
    name = name.lower()
    return NAME_FIXES.get(name, name).lower()


def _percent(text):
    return float(str(text).strip().rstrip("%") or 0)


def parse_csv(path):
    """Return ``[(pack_name, opened, {group: [(name, rarity, pct)]})]``."""
    with open(path, encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    header = rows[0]
    packs = []
    for base in range(0, len(header), 9):
        name = header[base].strip()
        if not name:
            continue
        opened = re.search(r"(\d+)", header[base + 1] if base + 1 < len(header)
                           else "")
        groups = {}
        for group, offset in GROUPS:
            entries = []
            for row in rows[2:]:
                col = base + offset
                if col + 2 >= len(row) or not row[col].strip():
                    continue
                entries.append((row[col].strip(), row[col + 1].strip(),
                                _percent(row[col + 2])))
            groups[group] = entries
        packs.append((name, int(opened.group(1)) if opened else 0, groups))
    return packs


def _pick(candidates, rarity, preference):
    """Pick one template row ``(guid, set_guid, rarity, ...)`` deterministically."""
    def rank(row):
        set_guid = (row[1] or "").lower()
        return (set_guid not in preference,
                preference.index(set_guid) if set_guid in preference else 0,
                row[2] != rarity, row[0])
    return sorted(candidates, key=rank)[0] if candidates else None


def build(csv_path, db_path):
    con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    try:
        cards, equipment, chests = {}, {}, {}
        for guid, set_guid, name, rarity in con.execute(
                "SELECT guid, set_guid, name, rarity FROM card_templates "
                "WHERE is_pve = 1"):
            cards.setdefault(_key(name), []).append((guid, set_guid, rarity, name))
        for guid, set_guid, name, rarity in con.execute(
                "SELECT guid, set_guid, name, rarity FROM equipment_templates"):
            equipment.setdefault(_key(name), []).append(
                (guid, set_guid, rarity, name))
        for guid, name in con.execute("SELECT guid, name FROM chest_templates"):
            chests.setdefault(_key(name), guid)
    finally:
        con.close()

    problems, notes, packs = [], [], {}
    for pack_name, opened, groups in parse_csv(csv_path):
        chest_guid = chests.get(PACK_NAME_ALIASES.get(_key(pack_name),
                                                      _key(pack_name)))
        if not chest_guid:
            problems.append(f"pack {pack_name!r}: no chest template")
            continue

        def resolve(group, table, preference):
            merged = {}
            for name, rarity, weight in groups[group]:
                row = _pick(table.get(_key(name), ()), rarity, preference)
                if row is None:
                    problems.append(f"{pack_name}: {group} {name!r} unresolved")
                    continue
                if row[2] != rarity:
                    notes.append(f"{pack_name}: {name!r} listed {rarity}, "
                                 f"template is {row[2]}")
                entry = merged.setdefault(row[0], {
                    "name": row[3], "guid": row[0], "rarity": row[2],
                    "weight": 0.0})
                entry["weight"] = round(entry["weight"] + weight, 4)
            return merged

        pve = resolve("pve_cards", cards, CARD_SET_PREFERENCE)
        gear = resolve("equipment", equipment, EQUIPMENT_SET_PREFERENCE)
        unique = resolve("unique_equipment", equipment, EQUIPMENT_SET_PREFERENCE)
        for guid, entry in unique.items():
            if guid not in gear:
                # The equipment column lists every drop; a unique piece it
                # misses still drops at its unique rate.
                notes.append(f"{pack_name}: unique {entry['name']!r} missing "
                             "from equipment drops; added at its unique rate")
                gear[guid] = dict(entry)
            gear[guid]["unique"] = True
        packs[chest_guid] = {
            "name": pack_name,
            "packs_opened": opened,
            "pve_cards": sorted(pve.values(), key=lambda e: e["name"].lower()),
            "equipment": sorted(gear.values(), key=lambda e: e["name"].lower()),
        }
    data = {"_source": SOURCE,
            "_comment": ("Generated by AssetExtraction/import_pack_loot_tables"
                         ".py; do not edit by hand. Keyed by chest template "
                         "GUID. 'equipment' is the per-slot pool (each pack "
                         "has two slots); 'unique' marks the dungeon's "
                         "exclusive pieces."),
            "packs": dict(sorted(packs.items()))}
    return data, problems, notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", required=True)
    parser.add_argument("--db", default=os.environ.get("HEX_DB_PATH",
                                                       os.path.join(ROOT, "hconnect.db")))
    parser.add_argument("--out", default=OUTPUT)
    args = parser.parse_args(argv)
    data, problems, notes = build(args.csv, args.db)
    for note in notes:
        print("note:", note)
    for problem in problems:
        print("UNRESOLVED:", problem, file=sys.stderr)
    if problems:
        return 1
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, ensure_ascii=False)
        stream.write("\n")
    print(f"wrote {len(data['packs'])} packs to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
