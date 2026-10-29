"""Regression coverage for the observed AZ1 campaign state machine.

These tests clone the local metadata database so campaign-state transitions can
be exercised without changing the developer's live character progress.
"""

import json
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

# Bind this process's test database before any runtime import
# opens ``db``; the live ``hconnect.db`` is never opened.
SRC = fresh_database()

import campaign
from campaign_fixtures import seed_campaign_fixtures


SAVAGE_LORD = "ab77df1e-5f13-471b-80e7-b7b4824ca280"
CORRUPT_DRYAD = "879317e1-8b04-486e-a10a-f2d2f1a080bc"
GNASH_BRIDGES = "3cf073b0-47fd-4911-953a-d86902890459"
HOWLING_PLAINS_PACK = "7b8390fd-7d3d-44d4-b285-1aeae3aef98b"
RAVENOUS_PIRANHA = "9ed0730b-e469-4c3d-ac7f-7b56c64b42ae"


def cloned_db():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    db = sqlite3.connect(path)
    source = sqlite3.connect(SRC)
    try:
        source.backup(db)
    finally:
        source.close()
    seed_campaign_fixtures(db)
    return db, path


def area_state(db, champion_id=6):
    raw = db.execute(
        "SELECT id, state_json FROM campaigns "
        "WHERE champion_id=? AND campaign_type='AREA' "
        "ORDER BY id DESC LIMIT 1", (champion_id,)).fetchone()
    assert raw, "the metadata database needs an AZ1 area campaign fixture"
    return raw[0], json.loads(raw[1])


def dungeon_fixture(db, champion_id=7):
    """Create a disposable Crayburn dungeon row and return its campaign ID."""
    camp_id = campaign._new_camp_id(db)
    state = campaign._build_initial_gameplay_state(
        camp_id, champion_id, "DUNGEON", "Shin'hare")
    db.execute(
        "INSERT INTO campaigns "
        "(id,camp_uid_lo,camp_uid_hi,champion_id,user_id,champion_name,"
        "template_name,campaign_type,is_started,state_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (camp_id, campaign._generate_inst_id(), 0, champion_id, 1,
         "test-shinhare", "Crayburn Castle", "DUNGEON", 1,
         json.dumps(state)),
    )
    db.commit()
    return camp_id


def test_cross_zila_objective_is_linked_to_savage_lord():
    db, path = cloned_db()
    try:
        raw = db.execute(
            "SELECT objectives_json FROM quest_templates "
            "WHERE script_name='az01_q_cross_the_river'"
        ).fetchone()[0]
        objective = next(
            item for item in json.loads(raw) if item.get("id") == "Step1")
        assert objective["type"] == "Encounter"
        assert objective["encounter"] == SAVAGE_LORD
    finally:
        db.close()
        os.unlink(path)


WINSTON_STEP1 = "81ab7bb5-21b4-4030-8e87-8fc4a206b1ab"


def test_winston_step1_starts_cross_the_river():
    """Issue #3: talking to Winston must start Cross the River."""
    db, path = cloned_db()
    try:
        rows = db.execute(
            "SELECT quest_script, node_id, role FROM quest_conversations "
            "WHERE conversation_guid=?", (WINSTON_STEP1,)).fetchall()
        assert rows == [("az01_q_cross_the_river", "Node012", "start")], rows
    finally:
        db.close()
        os.unlink(path)


def test_visited_winston_backfills_the_missing_cross_the_river_quest():
    """Issue #3: saves stuck after Winston get the quest on the next sync."""
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name=?", (6, "az01_q_cross_the_river"))
        db.commit()
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata["conversation_visits"] = {}
        campaign._sync_az1_quest_gates(db, 6, state)
        assert campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")[0] is None
        pdata["conversation_visits"] = {"Node012": 1}
        state["LastNode"] = "Node012"
        campaign._sync_az1_quest_gates(db, 6, state)
        assert campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")[0] is not None
        razortooth = next(x["Data"] for x in state["VisLocs"]
                          if x["Data"].get("node") == "Node014")
        assert razortooth["visible"] is True
    finally:
        db.close()
        os.unlink(path)

def test_area_gameend_keeps_the_hero_on_the_encounter_node():
    """Issue #1: an AREA result must not store the AZ0 trainer as LastNode."""
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        # The first AZ1 "Encounter" used to be the deck-less Usurper
        # entrance (now a Dungeon portal, issue #31).  Pick a battle that is
        # the first location with its name, so ALoc names exactly that node
        # (a battle behind its prelude conversation counts as well).
        names = [loc["Data"].get("name") for loc in state["VisLocs"]]
        encounter = next(loc["Data"] for index, loc in enumerate(state["VisLocs"])
                         if (loc["Data"].get("type") == "Encounter" or
                             loc["Data"].get("pre_encounter"))
                         and str(loc["Data"].get("node", "")).startswith("Node")
                         and names.index(loc["Data"].get("name")) == index)
        state["ALoc"] = encounter.get("name") or encounter["node"]
        state.pop("ActiveEncounterGuid", None)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        trainers = {cfg.get("trainer_npc")
                    for cfg in campaign._AZ0_RACE_CONFIG.values()}
        for won in (True, False):
            _camp_id, result = campaign._apply_gameend(db, area_id, won)
            assert result["LastNode"] == encounter["node"], result["LastNode"]
            assert not any(loc["Data"].get("node") in trainers
                           for loc in result["VisLocs"])
            assert "TrainingVictoryPending" not in result
    finally:
        db.close()
        os.unlink(path)


def test_savage_lord_defeat_conversation_keeps_the_encounter_open():
    """Issue #19: closing a defeat's result dialogue completed the node."""
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    original_push_campspawn = campaign.push_campspawn
    try:
        area_id, state = area_state(db)
        node = next(loc["Data"] for loc in state["VisLocs"]
                    if loc["Data"].get("node") == "Node014")
        node.update({"type": "Encounter", "encounter": SAVAGE_LORD,
                     "completed": False, "visible": True, "enabled": True})
        state["ALoc"] = node.get("name") or "Node014"
        state["ActiveEncounterGuid"] = SAVAGE_LORD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        _camp_id, result = campaign._apply_gameend(db, area_id, False)
        lost = next(loc["Data"] for loc in result["VisLocs"]
                    if loc["Data"].get("node") == "Node014")
        assert lost.get("outcome_conversation") and lost["type"] == "Convo", lost
        assert lost.get("outcome_won") is False, lost
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(result), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        reopened = next(loc["Data"] for loc in after["VisLocs"]
                        if loc["Data"].get("node") == "Node014")
        assert reopened.get("completed") is False, reopened
        assert not reopened.get("outcome_conversation"), reopened
        assert "Node014" in after["PublicState"]["Data"]["failed_nodes"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_failed_and_completed_encounters_are_reopened():
    """Issue #19: saves already closed by a defeat dialogue are repaired."""
    state = {
        "VisLocs": [
            {"Data": {"node": "Node014", "type": "Encounter",
                      "encounter": SAVAGE_LORD, "completed": True}},
            {"Data": {"node": "Node013", "type": "Encounter",
                      "encounter": "won", "completed": True}},
        ],
        "PublicState": {"Data": {"failed_nodes": ["Node014"]}},
    }
    campaign._reopen_failed_encounters(state)
    assert state["VisLocs"][0]["Data"]["completed"] is False
    assert state["VisLocs"][1]["Data"]["completed"] is True


def test_startloc_on_the_already_active_location_keeps_it_active():
    """Issue #11: ALoc stores the location name; re-entering the same node
    after a direct path arrival must not be treated as leaving it."""
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    original_push_campspawn = campaign.push_campspawn
    try:
        area_id, state = area_state(db)
        west = next(loc["Data"] for loc in state["VisLocs"]
                    if loc["Data"].get("node") == "Node015")
        west.update({"type": "Convo", "completed": False, "repeatable": False,
                     "visible": True, "enabled": True, "turninquest": True,
                     "conversationId": "5e5ec1cd-c869-43e8-82d4-417021954440"})
        state["ALoc"] = west["name"]
        state["LastNode"] = "Node015"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0, "Loc": west["name"], "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        assert after["ALoc"] == west["name"], after["ALoc"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_area_state_repair_drops_az0_trainer_leftovers():
    """Issue #1: saves that already stored the trainer are repaired."""
    trainer = next(iter(campaign._AZ0_RACE_CONFIG.values()))["trainer_npc"]
    state = {
        "LastNode": trainer, "ALoc": trainer, "TrainingVictoryPending": True,
        "VisLocs": [campaign._convo_location(trainer, "c"),
                    {"Data": {"node": "Node003", "name": "Dunnwood"}}],
        "PublicState": {"Data": {"visited_nodes": ["Node001", "Node003",
                                                   trainer]}},
    }
    campaign._repair_az0_leftovers_in_area(state)
    assert state["LastNode"] == "Node003"
    assert "ALoc" not in state and "TrainingVictoryPending" not in state
    assert [loc["Data"]["node"] for loc in state["VisLocs"]] == ["Node003"]

def test_node00r_uses_authored_node_r_conversation_fallback():
    db, path = cloned_db()
    try:
        rows = campaign._az1_node_conversation_rows(db, "NodeR")
        expected = next(
            guid for guid, _trigger, _priority, name in rows
            if "Step 1" in name
        )
        state = {"PublicState": {"Data": {"conversation_visits": {}}}}
        assert campaign._az1_node_conversation(
            db, "Node00R", state, champ_id=6) == expected
    finally:
        db.close()
        os.unlink(path)


def test_node00r_faction_quest_branch_opens_lena_grotto_and_stays_retryable():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_fort_romor", "az01_q_usurper"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_usurper")
        _area_id, state = area_state(db)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "completed": True, "repeatable": False,
            "visible": True, "enabled": True,
        })
        locations["Node025"].update({
            "completed": False, "visible": False, "enabled": False,
        })
        state["LastNode"] = "Node00R"
        state["ALoc"] = "Vale of Oberon"
        pdata = state["PublicState"]["Data"]
        pdata["quest_hidden_nodes"] = ["Node025"]
        pdata["blocked_nodes"] = ["Node025"]

        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)

        assert campaign._az1_node_conversation(
            db, "Node00R", state, champ_id=6) == \
            "8433f72e-bd47-4978-bc02-360e34ddce57"
        assert locations["Node00R"]["completed"] is False
        assert locations["Node00R"]["repeatable"] is True
        assert locations["Node025"]["visible"] is True
        assert locations["Node025"]["enabled"] is True
        assert "Node025" not in pdata["blocked_nodes"]
    finally:
        db.close()
        os.unlink(path)


def test_node00r_finished_faction_quest_uses_terminal_conversation():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_fort_romor", "az01_q_usurper"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_usurper")
        quest_id, quest_state = campaign._quest_state_row(
            db, 6, "az01_q_usurper")
        quest_state["Finished"] = "2026-09-09T00:00:00Z"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest_state), quest_id))
        db.commit()

        area_id, state = area_state(db)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "type": "Convo", "completed": True, "repeatable": False,
            "visible": True, "enabled": True,
        })
        state["LastNode"] = "Node00R"
        state["ALoc"] = "Vale of Oberon"
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node00R"]["conversationId"] == \
            "617819c9-bc9b-4788-a469-8117af28a613"
        assert locations["Node00R"]["completed"] is False
        assert locations["Node00R"]["repeatable"] is True
        assert locations["Node00R"]["quest_variant_terminal"] is True
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_node = next(
            item["Data"] for item in after["VisLocs"]
            if item["Data"].get("node") == "Node00R"
        )
        assert after_node["completed"] is True
        assert not after_node.get("quest_variant_terminal")
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_cross_zila_and_zodiac_gates_follow_quest_progress():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name IN (?, ?)",
            (6, "az01_q_cross_the_river", "az01_q_cross_the_river_part2"),
        )
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river_part2")

        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node012"],
            "visited_paths": [],
            "blocked_nodes": [],
            "unlocked_nodes": [],
            "quest_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "blockade_cleared": False,
        })
        state["LastNode"] = "Node012"
        state["ALoc"] = "Bridge over the Zila - East"

        # A fresh Tamed quest keeps both later branches hidden.
        tamed = campaign._quest_hook_az1_tamed_start(db, 6, state)
        assert tamed
        for node in ("Node015", "Node018"):
            location = next(x["Data"] for x in state["VisLocs"]
                            if x["Data"].get("node") == node)
            assert location["visible"] is False
            assert location["enabled"] is False

        # Starting Cross the Zila River reveals and marks Razortooth Forest.
        assert campaign._quest_hook_az1_cross_the_river_start(db, 6, state)
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        razortooth = next(x["Data"] for x in state["VisLocs"]
                          if x["Data"].get("node") == "Node014")
        assert razortooth["visible"] is True
        assert "Node014" in pdata["quest_nodes"]
        campaign._az1_reveal_neighbors(db, state, "Node012")
        assert "Path_Fork001_Node012" not in pdata["locked_paths"]
        assert "Path_Fork001_Node013" not in pdata["locked_paths"]
        assert "Path_Fork001_Node014" not in pdata["locked_paths"]

        # Cross the Zodiac is initially allowed to show Shadowgrove, but its
        # west bridge remains gated until Wallace's turn-in advances Step 2.
        db.execute(
            "UPDATE campaigns SET state_json=? WHERE id=?",
            (json.dumps(state), area_id),
        )
        db.commit()
        state["LastNode"] = "Node030"
        state["ALoc"] = "Bridge over the Zodiac - East"
        pdata["visited_nodes"] = ["Node030"]
        assert campaign._quest_hook_az1_cross_zodiac_start(db, 6, state)
        shadowgrove = next(x["Data"] for x in state["VisLocs"]
                           if x["Data"].get("node") == "Node018")
        west_bridge = next(x["Data"] for x in state["VisLocs"]
                           if x["Data"].get("node") == "Node038")
        assert shadowgrove["visible"] is True
        assert west_bridge["visible"] is False

        quest_id, raw = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river_part2")
        quest_state = json.loads(json.dumps(raw))
        quest_state["Flags"]["_quest_objective_idx"] = 2
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest_state), quest_id))
        db.commit()
        campaign._quest_hook_az1_cross_zodiac_start(db, 6, state)
        assert west_bridge["visible"] is True
        assert west_bridge["enabled"] is True

        # The opening Brink Ridge conversation only explains the blockade;
        # its northbound destinations remain hidden until the encounter wins.
        state["LastNode"] = "Node019"
        state["ALoc"] = "Brink Ridge"
        pdata["visited_nodes"] = ["Node019"]
        pdata["blockade_cleared"] = False
        campaign._az1_reveal_neighbors(db, state, "Node019")
        northbound = {
            node for node in campaign._az1_neighbors(db, "Node019")
            if node != "Node016"
        }
        for location in state["VisLocs"]:
            data = location["Data"]
            if data.get("node") in northbound:
                assert data["visible"] is False
    finally:
        db.close()
        os.unlink(path)


def test_az1_unfinished_encounter_does_not_reveal_outgoing_paths():
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node001", "Node002", "Node003"],
            "visited_paths": ["Path_Node001_Node002", "Path_Node002_Node003"],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node003"
        state["ALoc"] = "Dunnwood"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node002"].update({
            "completed": True, "autostart": False,
        })
        locations["Node003"].update({
            "type": "Encounter", "completed": False,
            "repeatable": False, "autostart": True,
            "pre_encounter_completed": True,
        })
        locations["Node004"].update({"visible": False, "enabled": False})
        locations["Node007"].update({"visible": False, "enabled": False})

        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node003"]["visible"] is True
        assert locations["Node004"]["visible"] is False
        assert locations["Node007"]["visible"] is False
        assert "Path_Node003_Node004" in pdata["locked_paths"]
        assert "Path_Node003_Node007" in pdata["locked_paths"]

        # A destination revealed by another route must not make the
        # unfinished node's own incoming path appear.
        pdata["unlocked_nodes"] = ["Node004"]
        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node004"]["visible"] is True
        assert "Path_Node003_Node004" in pdata["locked_paths"]

        # A normal victory opens the same outgoing paths.
        locations["Node003"].update({
            "completed": True, "autostart": False,
        })
        campaign._az1_reveal_neighbors(db, state, "Node003")
        assert locations["Node004"]["visible"] is True
        assert locations["Node007"]["visible"] is True
        assert "Path_Node003_Node004" not in pdata["locked_paths"]
        assert "Path_Node003_Node007" not in pdata["locked_paths"]

        # A concede must leave an encounter incomplete.  Conditional scenes
        # may be retryable after an unsuccessful *win*, but a loss cannot
        # promote this node to that state or expose paths beyond it.
        state["ActiveEncounterGuid"] = locations["Node003"]["encounter"]
        state["ALoc"] = "Dunnwood"
        locations["Node003"].update({
            "type": "Encounter", "completed": False,
            "repeatable": False, "autostart": True,
        })
        locations["Node004"].update({"visible": False, "enabled": False})
        locations["Node007"].update({"visible": False, "enabled": False})
        pdata["failed_nodes"] = []
        pdata["unlocked_nodes"] = []
        pdata["quest_reveal_nodes"] = []
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        _camp_id, after_loss = campaign._apply_gameend(db, area_id, False)
        after_locations = {
            item["Data"].get("node"): item["Data"]
            for item in after_loss["VisLocs"]
        }
        after_data = after_loss["PublicState"]["Data"]
        assert after_locations["Node003"]["completed"] is False
        assert after_locations["Node003"]["repeatable"] is False
        assert after_locations["Node004"]["visible"] is False
        assert after_locations["Node007"]["visible"] is False
        assert "Path_Node003_Node004" in after_data["locked_paths"]
        assert "Path_Node003_Node007" in after_data["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_unvisited_repeatable_conversations_are_not_blue():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata["conversation_visits"] = {}
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        for node in ("Node012", "Node016"):
            locations[node].update({
                "completed": False, "repeatable": True,
                "autostart": True, "visible": True, "enabled": True,
            })

        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node012"]["repeatable"] is False
        assert locations["Node016"]["repeatable"] is False

        # Once the authored conversation has actually closed, the same
        # repeatable content becomes the blue retryable state.
        pdata["conversation_visits"]["Node012"] = 1
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        assert locations["Node012"]["repeatable"] is True
    finally:
        db.close()
        os.unlink(path)


def test_az1_direct_node00r_route_is_not_replaced_by_fork_path():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node00R"],
            "visited_paths": ["Path_Node007_Node00R"],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
        })
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node00R"].update({
            "completed": True, "repeatable": False,
            "autostart": False, "visible": True, "enabled": True,
        })
        locations["Node012"].update({
            "completed": False, "repeatable": False,
            "autostart": True, "visible": False, "enabled": False,
        })
        locations["Fork001"].update({
            "visible": True, "enabled": False,
        })

        campaign._az1_reveal_neighbors(db, state, "Node00R")
        assert locations["Node012"]["visible"] is True
        assert "Path_Node00R_Node012" not in pdata["locked_paths"]
        assert "Path_Fork001_Node012" in pdata["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_alternate_reveal_does_not_unlock_incomplete_path():
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node009", "Node00R"],
            "visited_paths": [],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "unlocked_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node009"
        state["ALoc"] = "Node009"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node009"].update({
            "completed": False, "repeatable": False, "autostart": True,
        })
        locations["Node00R"].update({
            "completed": True, "repeatable": False, "autostart": False,
        })
        locations["Node012"].update({"visible": False, "enabled": False})

        # Node012 is revealed from completed Node00R, but the direct
        # Node009 -> Node012 path still belongs to the unfinished Node009
        # branch and must remain locked.
        campaign._az1_reveal_neighbors(db, state, "Node009")
        assert locations["Node012"]["visible"] is True
        assert "Path_Node009_Node012" in pdata["locked_paths"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_conv_done_recomputes_repeatable_node_paths():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node001", "Node002", "Node003", "Node007"],
            "visited_paths": [
                "Path_Node001_Node002", "Path_Node002_Node003",
                "Path_Node003_Node007",
            ],
            "locked_paths": [
                "Path_Node007_Node009", "Path_Node007_Node00R",
            ],
            "blocked_nodes": [],
            "quest_reveal_nodes": [],
            "quest_hidden_nodes": [],
            "unlocked_nodes": [],
            "failed_nodes": [],
        })
        state["LastNode"] = "Node007"
        state["ALoc"] = "The Road of Oaks"
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node003"].update({
            "completed": True, "repeatable": False, "autostart": False,
        })
        locations["Node007"].update({
            "type": "Convo", "completed": False, "repeatable": True,
            "autostart": True, "visible": True, "enabled": True,
        })
        locations["Node009"].update({"visible": False, "enabled": False})
        locations["Node00R"].update({"visible": False, "enabled": False})
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_data = after["PublicState"]["Data"]
        after_locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        assert after_locations["Node009"]["visible"] is True
        assert after_locations["Node00R"]["visible"] is True
        assert "Path_Node007_Node009" not in after_data["locked_paths"]
        assert "Path_Node007_Node00R" not in after_data["locked_paths"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_cross_zila_turnin_refreshes_bridge_path_and_weston_conversation():
    """A Winston turn-in must immediately open and activate Weston."""
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='az01_q_cross_the_river'", (6,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        area_id, state = area_state(db)
        state["LastNode"] = "Node012"
        state["ALoc"] = "Bridge over the Zila - East"
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({
            "visited_nodes": ["Node012"],
            "visited_paths": [],
            "blocked_nodes": [],
            "unlocked_nodes": ["Node015"],
            "quest_reveal_nodes": ["Node013", "Node014"],
            "quest_hidden_nodes": [],
            "failed_nodes": [],
            "conversation_visits": {"Node012": 1},
        })
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        locations["Node012"].update({
            "type": "Convo", "completed": False, "repeatable": True,
            "autostart": True, "conversationId":
            "ca7be3ef-66e8-4b08-bad2-9e1f36751573",
            "visible": True, "enabled": True,
        })
        locations["Node015"].update({
            "type": "Convo", "completed": False, "visible": True,
            "enabled": True,
        })

        # Put Cross the River at its Winston turn-in objective (Step 2).
        state["ActiveEncounterGuid"] = SAVAGE_LORD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        assert campaign._advance_quest_encounter_objectives(
            db, 6, SAVAGE_LORD)
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        after_data = after["PublicState"]["Data"]
        after_locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        _quest_id, quest_state = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")
        assert quest_state["Flags"]["_quest_objective_idx"] == 2
        assert "Path_Node012_Node015" not in after_data["locked_paths"]
        assert after_locations["Node015"]["visible"] is True
        assert after_locations["Node015"]["enabled"] is True
        assert after_locations["Node015"]["conversationId"] == \
            "5e5ec1cd-c869-43e8-82d4-417021954440"
        assert after_locations["Node015"]["turninquest"] is True

        # Entering the newly opened destination must preserve the active
        # quest conversation instead of replacing it with Weston Step 1b.
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Bridge over the Zila - West", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        entered = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        entered_weston = next(
            item["Data"] for item in entered["VisLocs"]
            if item["Data"].get("node") == "Node015")
        assert entered_weston["conversationId"] == \
            "5e5ec1cd-c869-43e8-82d4-417021954440"
        assert entered_weston["turninquest"] is True
        assert entered_weston["autostart"] is True
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_savage_lord_advances_cross_zila_and_node_rewards_are_authored():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='az01_q_cross_the_river'", (6,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        area_id, state = area_state(db)
        state["ActiveEncounterGuid"] = SAVAGE_LORD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        advanced = campaign._advance_quest_encounter_objectives(
            db, 6, SAVAGE_LORD)
        assert len(advanced) == 1
        _quest_id, progressed = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")
        assert progressed["Flags"]["_quest_objective_idx"] == 1
        assert progressed["VisLocs"][-1]["Data"]["node"] == "Step2"

        savage_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (SAVAGE_LORD,)).fetchone()[0])
        assert savage_rewards["gold"] == 300
        assert savage_rewards["xp"] == 200

        gnash_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (GNASH_BRIDGES,)).fetchone()[0])
        assert any(
            item.get("card_guid") == RAVENOUS_PIRANHA
            for item in gnash_rewards["end_of_game_rewards"]
        )
        dryad_rewards = json.loads(db.execute(
            "SELECT rewards_json FROM encounter_scenes WHERE guid=?",
            (CORRUPT_DRYAD,)).fetchone()[0])
        assert any(
            item.get("chest_guid") == HOWLING_PLAINS_PACK
            for item in dryad_rewards["end_of_game_rewards"]
        )
        assert any(
            item.get("xp") == 250 and item.get("gold") is None
            for item in dryad_rewards["end_of_game_rewards"]
        )
        assert any(
            item.get("item_guid_by_race", {}).get("Elf") ==
            "63e87fdf-6650-4861-90e5-7c8c840ec292"
            for item in dryad_rewards["end_of_game_rewards"]
        )
        conversation_rewards = dict(db.execute(
            "SELECT conversation_guid, reward_json FROM conversation_rewards "
            "WHERE conversation_guid IN (?, ?, ?, ?, ?)",
            ("5e5ec1cd-c869-43e8-82d4-417021954440",
             "08b9b8ab-2100-4f8d-87b0-18369eb4ecb4",
             "4000c850-c1f7-45e4-b254-3cb1b0e9bf2b",
             "02977c4c-803a-465d-ae0c-b5896c3d4012",
             "880690b0-0c54-4ab8-a4e2-911b8bf14f64"),
        ).fetchall())
        assert json.loads(
            conversation_rewards["5e5ec1cd-c869-43e8-82d4-417021954440"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        assert json.loads(
            conversation_rewards["08b9b8ab-2100-4f8d-87b0-18369eb4ecb4"]
        )["chest_guid"] == HOWLING_PLAINS_PACK
        for find_guid in (
            "02977c4c-803a-465d-ae0c-b5896c3d4012",
            "880690b0-0c54-4ab8-a4e2-911b8bf14f64",
        ):
            assert json.loads(conversation_rewards[find_guid])["chest_guid"] \
                == HOWLING_PLAINS_PACK
        assert json.loads(
            conversation_rewards["4000c850-c1f7-45e4-b254-3cb1b0e9bf2b"]
        ) == {}
    finally:
        db.close()
        os.unlink(path)


def test_corrupt_dryad_encounter_grants_pack_and_faction_equipment():
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        state["ActiveEncounterGuid"] = CORRUPT_DRYAD
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        class Handler:
            @staticmethod
            def _log_req(_message):
                pass

        result = campaign._apply_encounter_end_rewards(
            Handler(), db, SimpleNamespace(session_name="test", session_id=999),
            area_id, True)
        assert result["gold"] == 0
        assert result["xp"] == 250
        assert result["chests"]
        assert result["items"][0]["template"] == (
            "cd13fdb5-6d6f-4d8e-a413-589176b44935")  # Coyotle
        user_id = db.execute(
            "SELECT user_id FROM champions WHERE id=6").fetchone()[0]
        assert db.execute(
            "SELECT quantity FROM player_inventory "
            "WHERE user_id=? AND template_guid=?",
            (user_id, result["items"][0]["template"]),
        ).fetchone()[0] == 1
    finally:
        db.close()
        os.unlink(path)


def test_az1_scene_lookup_keeps_authored_node_variants_distinct():
    db, path = cloned_db()
    try:
        expected = {
            "Node018": "CORRUPT DRYAD",
            "Node06B": "SEA WITCH - NO FROG",
            "Node19B1": "ZOMBIE BLOCKADE",
            "Node23A_2": "HUMANS NO HELP",
            "Node75A": "DWARVEN DIGGERS",
        }
        for node, suffix in expected.items():
            scene = campaign._az1_scene_for_node(db, node)
            assert scene and suffix in scene[1], (node, scene)
    finally:
        db.close()
        os.unlink(path)


def test_az1_objectives_use_faction_specific_conversations():
    db, path = cloned_db()
    try:
        expected = {
            6: {
                "q_army_of_myth": "91f7eabb-eb17-4771-91b0-a5de3bee0126",
                "q_smoldering_dead": "0b30f528-ad70-480c-8ac4-d10a78b29fbb",
                "q_tranquil_dream": "c11fc1a7-cb27-4420-88dd-5c789c77b563",
                "q_devonshire_keep": "08677dda-e01e-4492-96a5-a36f4f4b6ca7",
            },
            4: {
                "q_army_of_myth": "0050adc1-bef2-45c9-9131-e80fcc80fd1a",
                "q_smoldering_dead": "78fc05bc-6503-4644-a84f-3109b7727ae7",
                "q_tranquil_dream": "0ec4b919-e18c-424a-96b0-310e3452aab3",
                "q_devonshire_keep": "abb502ef-fee9-4e44-b11a-d56d5fae6680",
            },
        }
        for champ_id, scripts in expected.items():
            for script, conversation in scripts.items():
                raw = db.execute(
                    "SELECT objectives_json FROM quest_templates "
                    "WHERE script_name=?", (script,)).fetchone()[0]
                objectives = campaign._materialize_quest_objectives(
                    db, champ_id, script, json.loads(raw))
                final = objectives[-1]
                assert final["conversation"] == conversation
                assert conversation in final["conversation_ids"]

        # The same persisted quest transition must accept the Underworld
        # completion conversation and reject the Ardent-only variant.
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='q_army_of_myth'", (4,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 4, "AREA", "q_army_of_myth")
        quest_id, state = campaign._quest_state_row(db, 4, "q_army_of_myth")
        state["Flags"]["_quest_objective_idx"] = 1
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), quest_id))
        db.commit()
        assert campaign._advance_quest_conversation_objectives(
            db, 4, "0050adc1-bef2-45c9-9131-e80fcc80fd1a")
        _quest_id, state = campaign._quest_state_row(db, 4, "q_army_of_myth")
        assert state["Finished"]
    finally:
        db.close()
        os.unlink(path)


def test_az1_opening_uses_the_underworld_route_and_fog_gates():
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type='AREA' OR template_name IN (?, ?))",
            (4, "az01_tamed", "az01_uw_find_cave_in"),
        )
        db.commit()
        _area_id, state = campaign._activate_az1_area(db, 4)
        spawned, _hooks = campaign._grant_quests_for_conversation(
            db, 4, "AZ1", "40def058-ae19-4fe5-ade8-4cc928d5b226")
        assert {script for _qid, script, _state in spawned} >= {
            "az01_tamed", "az01_uw_find_cave_in"
        }
        state = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE champion_id=? "
            "AND campaign_type='AREA' ORDER BY id DESC LIMIT 1", (4,)
        ).fetchone()[0])
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in state["VisLocs"]
        }
        assert locations["Node017"]["visible"] is True
        assert locations["Node017"]["enabled"] is True
        assert locations["Node034"]["visible"] is False
        assert locations["Node034"]["enabled"] is False
        assert locations["Node025"]["visible"] is False
    finally:
        db.close()
        os.unlink(path)


def test_az1_panorama_scene_transitions_out_of_area_on_startloc():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA', 'QUEST') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)

        # Put the player at the authored predecessor and expose Node017 as a
        # reachable destination, matching the map state immediately before
        # the client sends StartLoc for Cave-In.
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") == "Node016":
                data["completed"] = True
        state.update({"LastNode": "Node016", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Cave-In", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        area_state = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        assert area_state["ALoc"] is None

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        panorama_id, panorama_state = panorama_push[2], panorama_push[7]
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_state["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
        assert panorama_state["PublicState"]["Data"]["CampaignGroup"] == "AREA"
        assert panorama_state["PublicState"]["Data"]["HideQuickNavigation"] is False
        panorama_npcs = {
            item["Data"]["name"] for item in panorama_state["VisLocs"]
        }
        assert {"Ennis", "Takumi"}.issubset(panorama_npcs)
        assert not ({"Fahrny", "Myaa", "Vincent", "Wyatt", "Rizzix"}
                    & panorama_npcs)
        assert panorama_state["ALoc"] is None
        ennis = next(
            item["Data"] for item in panorama_state["VisLocs"]
            if item["Data"]["name"] == "Ennis")
        assert ennis["conversationId"] == \
            "4210aef3-52b5-40a1-a29a-b2e866579dcf"
        assert db.execute(
            "SELECT campaign_type FROM campaigns WHERE id=?",
            (panorama_id,)).fetchone()[0] == "PANORAMA"
        summary = campaign._build_camp_summary(
            panorama_id, 0, "PANORAMA", "AZ1", 6,
            panorama_state["PanoramaSceneGuid"], panorama_state["PanoramaNode"])
        assert summary["TypeInfo"]["AssetBundle"] == \
            "adventurezone01/p_dwrf_cavein"
        assert summary["TypeInfo"]["LevelPrefab"] == "p_dwrf_cavein"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_panorama_uses_authored_repeat_hint_when_taming_is_in_progress():
    db, path = cloned_db()
    try:
        row = db.execute(
            "SELECT id, state_json FROM campaigns "
            "WHERE champion_id=7 AND campaign_type='AREA' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert row
        panorama = campaign._build_az1_panorama_state(
            db, 14, 7,
            "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            json.loads(row[1]))
        locations = {
            item["Data"]["name"]: item["Data"]
            for item in panorama["VisLocs"]
        }
        assert locations["Takumi"]["conversationId"] == \
            "5b385410-6ca5-41ca-b4bb-63efc8f0a57d"
        assert locations["Takumi"]["turninquest"] is False
        assert locations["Takumi"]["repeatable"] is True
        assert locations["Ennis"]["conversationId"] == \
            "4210aef3-52b5-40a1-a29a-b2e866579dcf"

        campaign._ensure_quest_campaign(db, 6, "AREA", "az01_tamed")
        mesa = campaign._build_az1_panorama_state(
            db, 15, 6,
            "11e30dc4-542e-45a9-8b9d-2fddc48700b9", "Node034",
            json.loads(row[1]))
        mesa_locations = {
            item["Data"]["name"]: item["Data"]
            for item in mesa["VisLocs"]
        }
        assert mesa_locations["Belarius"]["conversationId"] == \
            "b8f1b954-1461-4374-b41a-77401f0a4eb6"
        assert mesa_locations["Kian"]["conversationId"] == \
            "aae342a7-3840-4b59-9291-6720ca9bc782"
    finally:
        db.close()
        os.unlink(path)


def test_az1_panorama_conversations_stay_visible_and_grant_new_quests():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campspawn = campaign.push_campspawn
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name IN ('az01_tamed', 'az01_uw_find_cave_in', "
            "'q_seawitch'))", (4,))
        db.commit()
        campaign._ensure_quest_campaign(db, 4, "AREA", "az01_tamed")
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, area_state = campaign._activate_az1_area(db, 4)
        panorama_id, panorama_state = campaign._activate_az1_panorama(
            db, 4, "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            area_state)

        # Closing the authored repeat/hint conversation must not make the
        # client hide Takumi.
        panorama_state["ALoc"] = "Takumi"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(panorama_state), panorama_id))
        db.commit()
        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campspawn = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after_takumi = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (panorama_id,)
        ).fetchone()[0])
        takumi = next(
            item["Data"] for item in after_takumi["VisLocs"]
            if item["Data"].get("name") == "Takumi")
        assert takumi["completed"] is False
        assert takumi["repeatable"] is True

        # Ennis is an explicit quest-start row. Closing it must create the
        # quest and immediately replace the start conversation with its
        # not-complete conversation instead of hiding the NPC.
        after_takumi["ALoc"] = "Ennis"
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(after_takumi), panorama_id))
        db.commit()
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "conv_done", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        _quest_id, sea_state = campaign._quest_state_row(db, 4, "q_seawitch")
        assert sea_state is not None
        after_ennis = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (panorama_id,)
        ).fetchone()[0])
        ennis = next(
            item["Data"] for item in after_ennis["VisLocs"]
            if item["Data"].get("name") == "Ennis")
        assert ennis["completed"] is False
        assert ennis["repeatable"] is True
        assert ennis["conversationId"] == \
            "a2eeb695-1ae8-49ce-b57a-3b7198a1ef92"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campspawn = original_push_campspawn
        db.close()
        os.unlink(path)


def test_az1_panorama_handoff_retries_on_arrived_node_without_encounter_field():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") == "Node017":
                data.update({
                    "visible": True,
                    "enabled": True,
                    "completed": True,
                    "encounter": None,
                })
        state.update({"LastNode": "Node017", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": area_id, "RAct": 0,
             "Loc": "Cave-In", "Params": []},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_ambling_mesa_uses_its_authored_panorama_asset():
    assert campaign._az1_panorama_assets("Node034", 6) == (
        "adventurezone01/p_cytl_amblingmesa", "p_cytl_amblingmesa")


def test_az1_panorama_return_parent_resumes_area_progress():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, area_state = campaign._activate_az1_area(db, 4)
        area_state["LastNode"] = "Node016"
        area_state["ALoc"] = None
        area_state["PublicState"]["Data"].update({
            "visited_nodes": ["Node001", "Node016"],
            "visited_paths": ["Path_Node015_Node016"],
        })
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(area_state), area_id))
        db.commit()
        panorama_id, _panorama_state = campaign._activate_az1_panorama(
            db, 4, "c99b4f8d-e24d-4896-9342-6ac2562a2364", "Node017",
            area_state)

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": panorama_id, "Event": "return_parent",
             "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        area_push = next(
            args for args in pushes if args[4] == "feralroot_travel")
        assert area_push[5] == "AREA"
        assert area_push[6] is True
        assert area_push[7]["LastNode"] == "Node016"
        assert area_push[7]["PublicState"]["Data"]["visited_paths"] == \
            ["Path_Node015_Node016"]
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_travel_button_transitions_completed_cave_in_on_visit_node():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") in {"Node016", "Node017"}:
                data["completed"] = True
        state.update({"LastNode": "Node016", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_node",
             "OParms": ["Node017"]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[5] == "PANORAMA"
        assert panorama_push[6] is True
        assert panorama_push[7]["PanoramaNode"] == "Node017"
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_multi_edge_travel_path_transitions_completed_cave_in():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node015", "Node016", "Node017"}:
                data.update({"visible": True, "enabled": True})
            if data.get("node") == "Node017":
                data["completed"] = True
        state.update({"LastNode": "Node015", "ALoc": None})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_path",
             "OParms": [["Path_Node015_Node016",
                         "Path_Node016_Node017"]]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        panorama_push = next(
            args for args in pushes if args[4] == "area_panorama")
        assert panorama_push[7]["PanoramaNode"] == "Node017"
        assert panorama_push[7]["PanoramaSceneGuid"] == \
            "c99b4f8d-e24d-4896-9342-6ac2562a2364"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_az1_repeated_path_from_cave_in_does_not_reopen_panorama():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND "
            "(campaign_type IN ('AREA', 'PANORAMA') OR "
            "template_name='az01_uw_find_cave_in')", (4,))
        db.commit()
        campaign._ensure_quest_campaign(
            db, 4, "AREA", "az01_uw_find_cave_in")
        area_id, state = campaign._activate_az1_area(db, 4)
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("node") in {"Node016", "Node017"}:
                data.update({
                    "visible": True, "enabled": True, "completed": True,
                })
        state.update({"LastNode": "Node017", "ALoc": "Cave-In"})
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.setdefault("visited_paths", []).append("Path_Node016_Node017")
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=4, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()

        pushes = []
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: pushes.append(args)
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_sendevent(
            handler, db,
            {"CampID": area_id, "Event": "visit_path",
             "OParms": [["Path_Node016_Node017"]]},
            0, "", 0, "ServiceCampaign", "253", 0, 0)

        assert not any(args[4] == "area_panorama" for args in pushes)
        after = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?", (area_id,)
        ).fetchone()[0])
        assert after["LastNode"] == "Node016"
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_crayburn_defeat_conversation_keeps_encounter_retryable():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_push_campupdate = campaign.push_campupdate
    try:
        camp_id = dungeon_fixture(db)
        state = campaign._build_initial_gameplay_state(
            camp_id, 7, "DUNGEON", "Shin'hare")
        state.update({
            "Started": "2026-09-07T00:00:00Z",
            "ALoc": "TowerGate",
            "LastNode": "TowerGate",
        })
        for node in campaign._CASTLE_CHAIN[1:5]:
            campaign._mark_location_completed(state, node)
        tower_gate = next(
            item["Data"] for item in state["VisLocs"]
            if item["Data"].get("node") == "TowerGate")
        tower_gate_fail = campaign._crayburn_node_data(
            "Shin'hare", "TowerGate")["fail"]
        tower_gate.update({
            "type": "Convo",
            "conversationId": tower_gate_fail,
            "encounter": None,
            "completed": False,
            "autostart": True,
        })
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()

        # This handler path normally writes a protocol response and pushes a
        # campaign update.  The state transition is all this regression test
        # needs to observe.
        campaign._send_response = lambda *args, **kwargs: None
        campaign.push_campupdate = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        event = {"CampID": camp_id, "Event": "conv_done", "OParms": None}
        campaign._handle_sendevent(
            handler, db, event, 0, "", 0, "ServiceCampaign", "253", 0, 0)

        raw = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0]
        after = json.loads(raw)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in after["VisLocs"]
        }
        assert locations["TowerGate"]["completed"] is False
        assert locations["TowerGate"]["type"] == "Encounter"
        assert locations["TowerGate"]["conversationId"] is None
        assert locations["TowerGate"]["encounter"] == \
            "c5cbbc95-a4ba-461e-9d42-1c592f120b1a"
        assert locations["PenworthTower"]["completed"] is False
        assert locations["PenworthTower"]["visible"] is False
        assert locations["PenworthTower"]["enabled"] is False
        assert after["ALoc"] == ""
        assert after["LastNode"] == "InnerBailey"

        # A duplicate/stale acknowledgement must not use LastNode to turn the
        # failed encounter into a win.
        campaign._handle_sendevent(
            handler, db, event, 0, "", 0, "ServiceCampaign", "253", 0, 0)
        raw = db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0]
        duplicate = json.loads(raw)
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in duplicate["VisLocs"]
        }
        assert locations["TowerGate"]["completed"] is False
        assert locations["PenworthTower"]["completed"] is False
    finally:
        campaign._send_response = original_send_response
        campaign.push_campupdate = original_push_campupdate
        db.close()
        os.unlink(path)


def test_crayburn_locked_future_node_does_not_replace_tower_gate_start():
    db, path = cloned_db()
    original_send_response = campaign._send_response
    original_launch_encounter = campaign._launch_encounter
    try:
        camp_id = dungeon_fixture(db)
        state = campaign._build_initial_gameplay_state(
            camp_id, 7, "DUNGEON", "Shin'hare")
        state.update({
            "Started": "2026-09-07T00:00:00Z",
            "ALoc": "",
            "LastNode": "InnerBailey",
        })
        for node in campaign._CASTLE_CHAIN[1:5]:
            campaign._mark_location_completed(state, node)
        # Simulate an old save that leaked the future marker to the client.
        penworth = next(
            item["Data"] for item in state["VisLocs"]
            if item["Data"].get("node") == "PenworthTower")
        penworth.update({"visible": True, "enabled": True})
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), camp_id))
        db.commit()

        campaign._send_response = lambda *args, **kwargs: None
        handler = SimpleNamespace(_log_req=lambda *_args: None)
        campaign._handle_locaction(
            handler, db,
            {"CampID": camp_id, "RAct": 0, "Loc": "PenworthTower"},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        after_reject = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0])
        locations = {
            item["Data"].get("node"): item["Data"]
            for item in after_reject["VisLocs"]
        }
        assert after_reject["ALoc"] == ""
        assert after_reject["LastNode"] == "InnerBailey"
        assert locations["PenworthTower"]["visible"] is False
        assert locations["PenworthTower"]["enabled"] is False

        # The legal destination remains Tower Gatehouse, and its authored
        # encounter is the one launched by the subsequent start event.
        campaign._handle_locaction(
            handler, db,
            {"CampID": camp_id, "RAct": 0, "Loc": "TowerGate"},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        active = json.loads(db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0])
        assert active["ALoc"] == "TowerGate"

        launched = []
        campaign._launch_encounter = lambda *args, **kwargs: launched.append(args[4])
        campaign._handle_sendevent(
            handler, db,
            {"CampID": camp_id, "Event": "start", "OParms": None},
            0, "", 0, "ServiceCampaign", "253", 0, 0)
        assert launched == ["c5cbbc95-a4ba-461e-9d42-1c592f120b1a"]
    finally:
        campaign._send_response = original_send_response
        campaign._launch_encounter = original_launch_encounter
        db.close()
        os.unlink(path)


def test_dungeon_win_count_tracks_streak_for_pregame_talents():
    """The "won the last encounter" dungeon talent uses a win streak.

    The PreGame talent must not fire on the first dungeon encounter merely
    because an unrelated non-dungeon battle was won earlier (the cumulative
    ``Wins`` counter).  ``_apply_gameend`` maintains a consecutive dungeon
    win streak in the campaign state, reset on a loss; the PreGame condition
    reads that streak.
    """
    from abilities.framework.conditions import _has_previous_dungeon_win
    db, path = cloned_db()
    try:
        camp_id = dungeon_fixture(db)
        session = SimpleNamespace(session_name=f"camp_{camp_id}")
        assert _has_previous_dungeon_win(db, session) is False
        campaign._apply_gameend(db, camp_id, True)
        assert _has_previous_dungeon_win(db, session) is True
        campaign._apply_gameend(db, camp_id, True)
        assert _has_previous_dungeon_win(db, session) is True
        campaign._apply_gameend(db, camp_id, False)
        assert _has_previous_dungeon_win(db, session) is False
    finally:
        db.close()
        os.unlink(path)


def test_post_crayburn_report_opens_the_az1_transition():
    """Finishing the Crayburn report conversation hands the race NPC the
    Feralroot travel conversation (every race; it used to crash on the
    four-column campaign row and drop the connection)."""
    import pve_db
    db, path = cloned_db()
    try:
        for race, cfg in campaign._AZ0_RACE_CONFIG.items():
            if not cfg.get("transition_conv"):
                continue
            champion_id = 9000 + race
            state = {"CurState": "EXPLORE", "ALoc": cfg["quest_npc"],
                     "PostCrayburnReport": True, "VisLocs": [
                         campaign._convo_location(cfg["intro_npc"], cfg["intro_conv"]),
                         campaign._convo_location(cfg["quest_npc"], "report",
                                                  turninquest=True)]}
            pano_id = campaign._new_camp_id(db)
            pve_db.db_create_campaign(pano_id, 1, champion_id, 1, "Tester",
                                      "AZ1", "PANORAMA", json.dumps(state), db,
                                      is_started=True)
            result = campaign._activate_az1_transition(db, champion_id, cfg)
            assert result, race
            assert result[0] == pano_id
            saved = json.loads(db.execute(
                "SELECT state_json FROM campaigns WHERE id=?",
                (pano_id,)).fetchone()[0])
            locs = {loc["Data"].get("node") or loc["Data"].get("name"): loc["Data"]
                    for loc in saved["VisLocs"]}
            assert locs[cfg["quest_npc"]]["completed"] is True, (race, locs)
            intro = locs[cfg["intro_npc"]]
            assert intro["conversationId"] == cfg["transition_conv"], (race, intro)
            assert intro["enabled"] and intro["visible"] and not intro["completed"]
            assert saved["ALoc"] is None and saved["CurState"] == "EXPLORE"
    finally:
        db.close()
        os.unlink(path)


def test_tamed_capture_pushes_the_quest_progress():
    """Winning a Tamed encounter with a captured troop advanced az01_tamed in
    the database but never pushed quest_progress, so the journal stayed on
    "Tame Dire Toad".  The next objective must also keep its encounter link."""
    import pvp_db
    db, path = cloned_db()
    saved = {name: getattr(campaign, name) for name in (
        "_apply_encounter_end_rewards", "push_gameendnotify",
        "push_campupdate", "advance_crayburn_step")}
    saved_identity = campaign.pve_db.db_campaign_identity_state
    saved_delete = pvp_db.db_delete_game_session
    pushed = []
    try:
        campaign._ensure_quest_campaign(db, 6, "AREA", "az01_tamed")
        campaign._apply_encounter_end_rewards = lambda *a, **k: {
            "condition_met": True, "applied": True,
            "scene_guid": "150b0ed9-a5e0-4729-81d8-764fa237c4e4"}
        campaign.push_gameendnotify = lambda *a, **k: None
        campaign.advance_crayburn_step = lambda *a, **k: None
        campaign.push_campupdate = lambda *a, **k: pushed.append(a)
        campaign.pve_db.db_campaign_identity_state =             lambda camp_id, conn=None: (6,)
        pvp_db.db_delete_game_session = lambda *a, **k: None
        handler = SimpleNamespace(_log_req=lambda *_a: None,
                                  user_profile={"id": 1})
        session = SimpleNamespace(session_name="camp_99", session_id=1)
        campaign.handle_battle_gameend(handler, db, session, True, 0)
        quest_pushes = [a for a in pushed if a[4] == "quest_progress"]
        assert len(quest_pushes) == 1, pushed
        state = quest_pushes[0][7]
        assert state["Flags"]["_quest_objective_idx"] == 1
        locs = {loc["Data"]["node"]: loc["Data"] for loc in state["VisLocs"]}
        assert locs["Tame1"]["completed"] is True
        assert locs["Tame2"]["encounter"] ==             "ddf8a2da-d397-4c3b-9d32-729f1bc86ade", locs["Tame2"]
    finally:
        for name, value in saved.items():
            setattr(campaign, name, value)
        campaign.pve_db.db_campaign_identity_state = saved_identity
        pvp_db.db_delete_game_session = saved_delete
        db.close()
        os.unlink(path)



WATER_ELEMENTAL = "a9e9e040-de92-475a-9970-be039c400cef"
FOUL_FAE = "f6f3074b-a416-4400-9b74-20d72406a846"
INDIGO_RIVER = "1d67638c-6068-465c-9e52-419047853310"


def test_bubblebee_objective_points_at_its_capture_scene():
    """Issue #22: the client data links "Tame Bubblebee" to Foul Fae, which
    has no Bubblebee; the capture battle is Node 31 Water Elemental."""
    import static
    db, path = cloned_db()
    try:
        objectives = json.loads(db.execute(
            "SELECT objectives_json FROM quest_templates "
            "WHERE script_name='az01_tamed'").fetchone()[0])
        links = {o["id"]: o.get("encounter") for o in objectives}
        assert links["Tame3"] == WATER_ELEMENTAL, links
        assert links["Tame1"] == "150b0ed9-a5e0-4729-81d8-764fa237c4e4", links
        stale = {"id": "Tame3", "title": "Tame Bubblebee and win match",
                 "type": "Encounter", "encounter": FOUL_FAE}
        assert static.tame_objective_scene(db, stale, FOUL_FAE) == WATER_ELEMENTAL

        def captures(scene):
            return any((r.get("end_of_game_condition") or {}).get("type") ==
                       "void_tamed_troop"
                       for r in campaign._scene_reward_records(db, scene))
        assert captures(WATER_ELEMENTAL)
        assert not captures(FOUL_FAE)
    finally:
        db.close()
        os.unlink(path)


def test_won_capture_node_reopens_while_its_objective_is_pending():
    """Issue #22: an old save completed Node031 although Tame Bubblebee is
    still the active objective (stored with the stale Foul Fae link)."""
    db, path = cloned_db()
    try:
        campaign._ensure_quest_campaign(db, 6, "AREA", "az01_tamed")
        quest_id, raw = campaign.pve_db.db_campaign_state_rows(
            6, "QUEST", db, template_name="az01_tamed")[0]
        quest = json.loads(raw)
        flags = quest.setdefault("Flags", {})
        objectives = flags.get("_quest_objectives") or []
        tame3 = next(o for o in objectives if o.get("id") == "Tame3")
        tame3["encounter"] = FOUL_FAE
        flags["_quest_objectives"] = objectives
        flags["_quest_objective_idx"] = objectives.index(tame3)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest), quest_id))
        db.commit()
        state = {"VisLocs": [{"Data": {
            "node": "Node031", "type": "Encounter", "encounter": WATER_ELEMENTAL,
            "completed": True, "repeatable": False}}, {"Data": {
            "node": "Node029", "type": "Encounter", "encounter": FOUL_FAE,
            "completed": True, "repeatable": False}}]}
        campaign._reopen_pending_quest_encounters(db, 6, state)
        node31, other = (loc["Data"] for loc in state["VisLocs"])
        assert node31["completed"] is False and node31["repeatable"] is True
        assert other["completed"] is True
    finally:
        db.close()
        os.unlink(path)


def test_indigo_river_prelude_is_a_conversation_not_a_battle():
    """Issue #23: Node033's scene has no AI deck; its only conversation is
    the whole visit.  Promoting it to an Encounter crashed the client."""
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        locs = {loc["Data"].get("node"): loc["Data"] for loc in state["VisLocs"]}
        fresh = dict(locs["Node033"], type="Encounter", encounter=None,
                     completed=False, conversationId=None)
        for key in ("pre_encounter", "pre_encounter_completed"):
            fresh.pop(key, None)
        stuck = dict(fresh, encounter=INDIGO_RIVER, pre_encounter=True,
                     pre_encounter_completed=True, autostart=True)
        battle = dict(locs["Node009"], type="Encounter", completed=False,
                      conversationId=None)
        for key in ("pre_encounter", "pre_encounter_completed"):
            battle.pop(key, None)
        rows = [{"Data": fresh}, {"Data": stuck}, {"Data": battle}]
        campaign._hydrate_az1_area_scene_metadata(db, rows)
        fresh, stuck, battle = (row["Data"] for row in rows)
        assert fresh["type"] == "Convo" and fresh["encounter"] is None, fresh
        assert fresh["conversationId"] and fresh["autostart"] is True, fresh
        assert not fresh.get("pre_encounter"), fresh
        assert stuck["type"] == "Convo" and stuck["completed"] is True, stuck
        assert stuck["encounter"] is None and not stuck["autostart"], stuck
        # A real battle with a prelude keeps its pre-encounter flow.
        assert battle.get("pre_encounter") is True, battle
    finally:
        db.close()
        os.unlink(path)


def test_campaign_game_end_cleans_up_once():
    """Issue #8: the campaign game end raised NameError before cleaning the
    session, so a second result pass re-ran the Crayburn step and finished
    the dungeon behind the victory conversation."""
    import pvp_db
    db, path = cloned_db()
    saved = {name: getattr(campaign, name) for name in (
        "_apply_encounter_end_rewards", "push_gameendnotify",
        "push_campupdate", "advance_crayburn_step")}
    saved_identity = campaign.pve_db.db_campaign_identity_state
    saved_delete = pvp_db.db_delete_game_session
    deleted, advanced = [], []
    try:
        campaign._apply_encounter_end_rewards = lambda *a, **k: {
            "applied": True, "scene_guid": ""}
        campaign.push_gameendnotify = lambda *a, **k: None
        campaign.push_campupdate = lambda *a, **k: None
        campaign.advance_crayburn_step = lambda *a, **k: advanced.append(a)
        campaign.pve_db.db_campaign_identity_state = \
            lambda camp_id, conn=None: (6,)
        pvp_db.db_delete_game_session = lambda sid, *a, **k: deleted.append(sid)
        handler = SimpleNamespace(_log_req=lambda *_a: None,
                                  user_profile={"id": 1})
        session = SimpleNamespace(session_name="camp_99", session_id=42)
        assert campaign.handle_battle_gameend(handler, db, session, True, 0)
        assert campaign.handle_battle_gameend(handler, db, session, True, 0)
        assert deleted == [42], deleted
        assert len(advanced) == 1, advanced
    finally:
        for name, value in saved.items():
            setattr(campaign, name, value)
        campaign.pve_db.db_campaign_identity_state = saved_identity
        pvp_db.db_delete_game_session = saved_delete
        db.close()
        os.unlink(path)


def test_fortune_of_blood_resolves_both_authored_abilities():
    """Issue #9: Fortune of Blood lists its random-threshold ability twice."""
    db, path = cloned_db()
    try:
        blood = db.execute("SELECT guid FROM card_templates "
                           "WHERE name='Fortune of Blood'").fetchone()[0]
        guids = campaign._fortune_ability_guids(db, blood)
        assert len(guids) == 2 and len(set(guids)) == 1, guids
        fortunes = campaign._fortune_card_guids(db)
        assert len(fortunes) == 10, fortunes
        assert all(campaign._fortune_ability_guids(db, guid)
                   for guid in fortunes)
    finally:
        db.close()
        os.unlink(path)


def test_only_closing_quest_conversations_are_turn_ins():
    """Issue #11: "Return to Winston" (mid-quest, no reward) is not a turn-in.

    The client waits for a reward after a QuestTurnIn conversation and neither
    lowers the Zila bridge nor starts Weston until it arrives."""
    db, path = cloned_db()
    try:
        db.execute(
            "DELETE FROM campaigns WHERE champion_id=? AND campaign_type='QUEST' "
            "AND template_name='az01_q_cross_the_river'", (6,))
        db.commit()
        assert campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river")
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata["unlocked_nodes"] = ["Node015"]
        assert campaign._advance_quest_encounter_objectives(
            db, 6, SAVAGE_LORD)
        campaign._apply_az1_quest_markers(db, 6, state)
        locations = {item["Data"].get("node"): item["Data"]
                     for item in state["VisLocs"]}
        assert locations["Node012"]["turninquest"] is False, locations["Node012"]
        quest_id, quest_state = campaign._quest_state_row(
            db, 6, "az01_q_cross_the_river")
        quest_state["Flags"]["_quest_objective_idx"] = 2
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest_state), quest_id))
        db.commit()
        campaign._apply_az1_quest_markers(db, 6, state)
        assert locations["Node015"]["turninquest"] is True, locations["Node015"]
    finally:
        db.close()
        os.unlink(path)


def test_unlocked_west_bridge_visit_survives_the_tamed_gate():
    """Issue #11: the Tamed gate purged Node015 even after it was unlocked."""
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({"unlocked_nodes": ["Node015"],
                      "visited_nodes": ["Node012", "Node015"],
                      "visited_paths": ["Path_Node012_Node015"],
                      "conversation_visits": {"Node015": 1}})
        campaign._quest_hook_az1_tamed_start(db, 6, state)
        assert "Node015" in pdata["visited_nodes"]
        assert "Path_Node012_Node015" in pdata["visited_paths"]
        assert pdata["conversation_visits"].get("Node015") == 1
        west = next(x["Data"] for x in state["VisLocs"]
                    if x["Data"].get("node") == "Node015")
        assert west.get("visible") is not False, west
        pdata["unlocked_nodes"] = []
        campaign._quest_hook_az1_tamed_start(db, 6, state)
        assert "Node015" not in pdata["visited_nodes"]
    finally:
        db.close()
        os.unlink(path)

def test_finished_cross_zodiac_keeps_the_west_bridge_open():
    """Issue #27: once Cross the Zodiac River finished, the Tamed hook took
    it for "not started" and hid Node038 under the player again."""
    db, path = cloned_db()
    try:
        campaign._ensure_quest_campaign(
            db, 6, "AREA", "az01_q_cross_the_river_part2")
        quest_id, raw = campaign.pve_db.db_campaign_state_rows(
            6, "QUEST", db, template_name="az01_q_cross_the_river_part2")[0]
        quest = json.loads(raw)
        quest["Finished"] = "2026-10-28T13:31:50Z"
        quest.setdefault("Flags", {})["_quest_objective_idx"] = 3
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(quest), quest_id))
        db.commit()
        _area_id, state = area_state(db)
        pdata = state.setdefault("PublicState", {}).setdefault("Data", {})
        pdata.update({"blocked_nodes": ["Node038", "Node018"],
                      "quest_hidden_nodes": ["Node038", "Node018"]})
        for loc in state["VisLocs"]:
            if loc["Data"].get("node") in ("Node038", "Node018"):
                loc["Data"].update({"visible": False, "enabled": False})
        campaign._quest_hook_az1_tamed_start(db, 6, state)
        for node in ("Node038", "Node018"):
            assert node not in pdata["blocked_nodes"], pdata["blocked_nodes"]
            assert node not in pdata["quest_hidden_nodes"], pdata
        dryad = next(x["Data"] for x in state["VisLocs"]
                     if x["Data"].get("node") == "Node018")
        assert dryad.get("visible") is True, dryad
    finally:
        db.close()
        os.unlink(path)


DRYAD_SCENE = "879317e1-8b04-486e-a10a-f2d2f1a080bc"
WALLACE = {"step1": "5b36ea00-d06a-4b97-9836-89aaa52fb7ee",
           "step2": "9f414188-5337-464d-9521-a89f37fcee9c",
           "step3": "1e9d1bd0-0967-4fe8-be79-d0cb068f973e",
           "step4": "5061b7f4-9c7c-4e2f-a216-9924928c5db7"}
WARREN_BEATEN = "08b9b8ab-2100-4f8d-87b0-18369eb4ecb4"
WARREN_NOT_BEATEN = "48187c2d"


def _set_cross_zodiac(db, champ, index, finished=False):
    campaign._ensure_quest_campaign(
        db, champ, "AREA", "az01_q_cross_the_river_part2")
    quest_id, raw = campaign.pve_db.db_campaign_state_rows(
        champ, "QUEST", db, template_name="az01_q_cross_the_river_part2")[0]
    quest = json.loads(raw)
    flags = quest.setdefault("Flags", {})
    if not flags.get("_quest_objectives"):
        flags["_quest_objectives"] = json.loads(db.execute(
            "SELECT objectives_json FROM quest_templates "
            "WHERE script_name='az01_q_cross_the_river_part2'").fetchone()[0])
    flags["_quest_objective_idx"] = index
    quest["Finished"] = "2026-10-28T13:31:50Z" if finished else None
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(quest), quest_id))
    db.commit()


def test_wallace_follows_the_story_order_on_revisits():
    """Issue #25: revisits picked Wallace's turn-in by GUID order before the
    Dryad was beaten."""
    db, path = cloned_db()
    try:
        def pick(visits, steps, beaten):
            state = {"PublicState": {"Data": {
                "conversation_visits": {"Node030": visits},
                "conversation_steps": steps}},
                "_encounter_successes": {DRYAD_SCENE: 1} if beaten else {},
                "VisLocs": []}
            return campaign._az1_node_conversation(
                db, "Node030", state, champ_id=6)

        _set_cross_zodiac(db, 6, 0)
        assert pick(0, {}, False) == WALLACE["step1"]
        assert pick(1, {"Node030": 1}, False) == WALLACE["step2"]
        assert pick(2, {"Node030": 2}, False) == WALLACE["step2"]
        _set_cross_zodiac(db, 6, 1)
        assert pick(3, {"Node030": 2}, True) == WALLACE["step3"]
        _set_cross_zodiac(db, 6, 2)
        assert pick(4, {"Node030": 3}, True) == WALLACE["step4"]
        _set_cross_zodiac(db, 6, 3, finished=True)
        assert pick(5, {"Node030": 4}, True) == WALLACE["step4"]
    finally:
        db.close()
        os.unlink(path)


def test_warren_variant_follows_the_dryad_outcome():
    """Issue #25: "Step 1a player has beaten Dryad" sorted first and played
    regardless of the Dryad fight."""
    db, path = cloned_db()
    try:
        def pick(beaten):
            state = {"PublicState": {"Data": {"conversation_visits": {}}},
                     "_encounter_successes": {DRYAD_SCENE: 1} if beaten else {},
                     "VisLocs": []}
            return campaign._az1_node_conversation(
                db, "Node038", state, champ_id=6)

        _set_cross_zodiac(db, 6, 0)
        assert pick(False).startswith(WARREN_NOT_BEATEN), pick(False)
        _set_cross_zodiac(db, 6, 2)
        assert pick(True) == WARREN_BEATEN
    finally:
        db.close()
        os.unlink(path)


TRANQUIL_DREAM = "d657fcad-9dbe-4acc-bb62-2495fd4a4628"
TRANQUIL_DREAM_ENTRANCE = "66be7540-866b-496b-bbf5-d33bdfb05ab1"
SISTER_MIDNIGHT_GREETING = "d14cc045-1427-4ad6-94ef-9c1629ab1bb2"
TIPI_CONVERSATION = "1aa34077-f97a-4cdd-8bb7-3644befd8954"
AZ1_DUNGEON_ENTRANCES = ("Node008", "Node011", "Node024", "Node040", "Node068")
_CONVERSATION_NAMES = {}


def _conversation_names():
    """Every ConversationTemplate the client knows (GUID -> name)."""
    if not _CONVERSATION_NAMES:
        from AssetExtraction import gamedata_seed
        if gamedata_seed.configured_path():
            data = gamedata_seed.load_text()
        else:
            data = gamedata_seed.load_records_text(
                gamedata_seed.configured_records_path())
        _CONVERSATION_NAMES.update(gamedata_seed._conversation_names(data))
    return _CONVERSATION_NAMES


class _DungeonClient:
    """Drives campaign.py through the dungeon client protocol."""

    def __init__(self, db):
        self.db = db
        self.pushes = []
        self.launched = []
        self.handler = SimpleNamespace(_log_req=lambda *_args: None)
        self._saved = {name: getattr(campaign, name) for name in (
            "_send_response", "push_campupdate", "_launch_encounter")}
        campaign._send_response = lambda *args, **kwargs: "response"
        campaign.push_campupdate = lambda *args, **kwargs: \
            self.pushes.append(args)
        campaign._launch_encounter = lambda *args, **kwargs: \
            self.launched.append(args[4])

    def restore(self):
        for name, value in self._saved.items():
            setattr(campaign, name, value)

    def state(self, camp_id):
        return json.loads(self.db.execute(
            "SELECT state_json FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()[0])

    def _args(self):
        return (0, "", 0, "ServiceCampaign", "253", 0, 0)

    def startloc(self, camp_id, location):
        campaign._handle_locaction(
            self.handler, self.db,
            {"CampID": camp_id, "RAct": 0, "Loc": location, "Params": []},
            *self._args())
        return self.state(camp_id)

    def event(self, camp_id, name, params=None):
        campaign._handle_sendevent(
            self.handler, self.db,
            {"CampID": camp_id, "Event": name, "OParms": params},
            *self._args())
        return self.state(camp_id)

    def startcamp(self, camp_id):
        campaign._handle_startcamp(
            self.handler, self.db, {"CampID": camp_id}, *self._args())
        return self.state(camp_id)

    def battle(self, camp_id, won):
        """Press Start on the battle panel, then finish the battle."""
        self.event(camp_id, "start")
        campaign._apply_gameend(self.db, camp_id, won)
        campaign.advance_crayburn_step(
            self.handler, self.db, camp_id, won, 0, "", "ServiceCampaign",
            "253", 0, 0, auto_activate=False)
        return self.state(camp_id)


def _assert_client_contract(db, state):
    """Every location must be renderable by the client's dungeon VM."""
    catalog = {item["Name"]: item["Data"]["encscene"]
               for item in state.get("Encounters") or []}
    names = _conversation_names()
    nodes = {item["Name"] for item in state.get("LocNodes") or []}
    for loc in state["VisLocs"]:
        data = loc["Data"]
        assert data["node"] in nodes, data
        if data["type"] == "Encounter":
            scene = campaign.pve_db.db_encounter_scene_runtime(
                data["encounter"], db)
            assert scene and scene[4] and scene[5], data
            assert catalog.get(data["encounter"]) == data["encounter"], data
        elif data["type"] == "Convo":
            assert data["conversationId"] in names, data
        else:
            assert data["type"] in ("Empty", "Dungeon"), data
            assert not data.get("encounter"), data


def _enter_dungeon(db, client, champion_id, node):
    """Walk the AZ1 map to a dungeon entrance and press Enter."""
    area_id, state = area_state(db, champion_id)
    for loc in state["VisLocs"]:
        data = loc["Data"]
        if data.get("node") == node:
            data.update({"visible": True, "enabled": True})
            name = data["name"]
    state.update({"LastNode": node, "ALoc": None})
    campaign._hydrate_az1_area_scene_metadata(
        db, state["VisLocs"], champ_id=champion_id, state=state)
    db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
               (json.dumps(state), area_id))
    db.commit()
    client.startloc(area_id, name)
    push = next(args for args in client.pushes if args[4] == "area_dungeon")
    assert push[5] == "DUNGEON" and push[6] is True, push[4:7]
    return area_id, push[2]


def _walk_dungeon(db, client, camp_id, lose_first=True, limit=200):
    """Play a run to its end; returns the conversations that were played."""
    played = []
    state = client.startcamp(camp_id)
    lost_once = not lose_first
    for _step in range(limit):
        _assert_client_contract(db, state)
        if state.get("Finished"):
            return state, played
        active = next((loc["Data"] for loc in state["VisLocs"]
                       if loc["Data"]["node"] == state.get("ALoc")), None)
        if active is None:
            # Back on the map: travel to the next reachable room.
            candidates = [loc["Data"] for loc in state["VisLocs"]
                          if loc["Data"]["visible"] and loc["Data"]["enabled"]
                          and not loc["Data"]["completed"]]
            assert candidates, ("stuck", state["VisLocs"])
            state = client.startloc(camp_id, candidates[0]["name"])
            continue
        if active["type"] == "Convo":
            played.append(active["conversationId"])
            state = client.event(camp_id, "conv_done")
        elif active["type"] == "Encounter":
            won = lost_once
            lost_once = True
            state = client.battle(camp_id, won)
            assert client.launched[-1] == active["encounter"]
            if not won:
                # The fail conversation plays, then the battle is retryable.
                fail = next(loc["Data"] for loc in state["VisLocs"]
                            if loc["Data"]["node"] == active["node"])
                if fail["type"] == "Convo":
                    assert state["ALoc"] == active["node"], state["ALoc"]
                    played.append(fail["conversationId"])
                    state = client.event(camp_id, "conv_done")
                retry = next(loc["Data"] for loc in state["VisLocs"]
                             if loc["Data"]["node"] == active["node"])
                assert retry["type"] == "Encounter", retry
                assert retry["encounter"] == active["encounter"], retry
                assert not retry["completed"] and not retry["autostart"], retry
                assert state["ALoc"] == "", state["ALoc"]
        else:
            state = client.event(camp_id, "empty_done")
    raise AssertionError("dungeon run did not finish")


def test_dungeon_entrances_are_never_deckless_battles():
    """Issue #31: AZ1 dungeon entrances have no AI deck; sending them as an
    Encounter crashed the client in EncounterScene.GetChampionTemplate."""
    db, path = cloned_db()
    try:
        _area_id, state = area_state(db)
        locs = {loc["Data"].get("node"): loc["Data"]
                for loc in state["VisLocs"]}
        for node in AZ1_DUNGEON_ENTRANCES:
            assert locs[node]["type"] in ("Dungeon", "Empty"), locs[node]
            assert locs[node]["encounter"] is None, locs[node]
        assert locs["Node040"]["type"] == "Dungeon", locs["Node040"]
        assert locs["Node040"]["dungeon_template"] == TRANQUIL_DREAM
        assert locs["Node040"]["repeatable"] is True
        # Any other deck-less battle is guarded as well.
        stale = [{"Data": dict(locs["Node039"], type="Encounter",
                               encounter=TRANQUIL_DREAM_ENTRANCE)}]
        campaign._guard_deckless_encounters(db, stale)
        assert stale[0]["Data"]["type"] == "Empty", stale
        assert stale[0]["Data"]["encounter"] is None, stale
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data.get("type") == "Encounter":
                scene = campaign.pve_db.db_encounter_scene_runtime(
                    data.get("encounter"), db)
                assert scene and scene[4], data
    finally:
        db.close()
        os.unlink(path)


def test_tranquil_dream_entrance_starts_its_dungeon():
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        area_id, camp_id = _enter_dungeon(db, client, 6, "Node040")
        row = db.execute(
            "SELECT campaign_type, template_name FROM campaigns WHERE id=?",
            (camp_id,)).fetchone()
        assert row == ("DUNGEON", "The Tranquil Dream"), row
        assert client.state(area_id)["ALoc"] is None
        summary_row = campaign.pve_db.db_campaign_summary_row(camp_id, db)
        state = json.loads(summary_row[4])
        summary = campaign._build_camp_summary(
            camp_id, 0, "DUNGEON", "The Tranquil Dream", 3,
            dungeon_run=state["DungeonRun"])
        assert summary["TypeInfo"]["NodesPrefab"] == \
            "campaign/az01/tranquildream/prefabs/nodes"
        assert summary["TypeInfo"]["BackgroundPrefab"] == \
            "campaign/az01/tranquildream/prefabs/background"
        assert summary["TypeInfo"]["CampaignTemplateId"] == TRANQUIL_DREAM
        assert state["Started"] is None and state["ALoc"] == ""
        rooms = {room["title"]: room for room in state["DungeonRun"]["rooms"]}
        for title in ("Dream Turtle", "Dream Bear", "Dream Eagle",
                      "Dream Skarn", "Dream Spider"):
            stage = rooms[title]["stages"][0]
            assert stage["preludes"] and stage["encounter"], (title, stage)
            assert stage["success"] and stage["fail"], (title, stage)
        boss = rooms["Sister Midnight"]["stages"][0]
        assert campaign.pve_db.db_encounter_scene_runtime(
            boss["encounter"], db)[2] == "Sister Midnight", boss
        state = client.startcamp(camp_id)
        assert state["ALoc"] == "NodeA", state["ALoc"]
        tipi = next(loc["Data"] for loc in state["VisLocs"]
                    if loc["Data"]["node"] == "NodeA")
        assert tipi["type"] == "Convo", tipi
        assert tipi["conversationId"] == TIPI_CONVERSATION, tipi
        hidden = {loc["Data"]["node"] for loc in state["VisLocs"]
                  if not loc["Data"]["visible"]}
        assert {"NodeB", "NodeG"} <= hidden, hidden
        _assert_client_contract(db, state)
        # The Dream hub: Sister Midnight greets before any animal is shown.
        state = client.event(camp_id, "conv_done")
        state = client.startloc(camp_id, "NodeG")
        assert state["ALoc"] == "NodeG", state["ALoc"]
        hub = next(loc["Data"] for loc in state["VisLocs"]
                   if loc["Data"]["node"] == "NodeG")
        assert hub["type"] == "Convo", hub
        assert hub["conversationId"] == SISTER_MIDNIGHT_GREETING, hub
        animals = {"NodeB", "NodeC", "NodeD", "NodeE", "NodeF"}
        visible = {loc["Data"]["node"] for loc in state["VisLocs"]
                   if loc["Data"]["visible"]}
        assert not animals & visible, visible
        state = client.event(camp_id, "conv_done")
        visible = {loc["Data"]["node"] for loc in state["VisLocs"]
                   if loc["Data"]["visible"]}
        assert animals <= visible, visible
        # Reconnecting keeps the active run.
        assert campaign._get_existing_campaign_for_champion(
            db, 6, "DUNGEON")[0] == camp_id
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_tranquil_dream_run_completes_its_quest_objective():
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        campaign._ensure_quest_campaign(db, 6, "AREA", "q_tranquil_dream")
        area_id, camp_id = _enter_dungeon(db, client, 6, "Node040")
        state, played = _walk_dungeon(db, client, camp_id)
        assert state["FinishReason"] == "Complete", state
        names = _conversation_names()
        played_names = [names[guid] for guid in played]
        assert played_names[0] == "Tranquil Dream - Wigwam", played_names
        # Arriving in the Dream plays Sister Midnight's greeting first; the
        # finale keeps only her second speech.
        assert played_names[1] == "Tranquil Dream - Sister Midnight", played_names
        assert played_names.count("Tranquil Dream - Sister Midnight") == 1
        for animal in ("Turtle", "Bear", "Eagle", "Skarn", "Spider"):
            assert (f"Tranquil Dream - The Dream {animal} Success"
                    in played_names), (animal, played_names)
        assert any(name.endswith(" Fail") for name in played_names)
        assert played_names[-2:] == [
            "Tranquil Dream - Sister Midnight 2 Success",
            "Tranquil Dream - Wigwam 2"], played_names
        quest_id, quest = campaign._quest_state_row(db, 6, "q_tranquil_dream")
        assert quest["Flags"]["_quest_objective_idx"] == 1, quest["Flags"]
        assert any(args[4] == "quest_progress" and args[2] == quest_id
                   for args in client.pushes)
        back = next(args for args in client.pushes
                    if args[4] == "dungeon_complete")
        assert back[5] == "AREA" and back[6] is True and back[2] == area_id
        area = client.state(area_id)
        entrance = next(loc["Data"] for loc in area["VisLocs"]
                        if loc["Data"]["node"] == "Node040")
        assert entrance["type"] == "Dungeon" and entrance["completed"]
        assert area["LastNode"] == "Node040"
        assert campaign.pve_db.db_champion_last_campaign_id(6, db) == area_id
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_tranquil_dream_answers_pick_the_battle_and_boss_form():
    """The animal talks score 1-3 points (ServerScriptEvent): the answer
    picks the animal's Easy/Medium/Hard battle and the run's total picks
    Sister Midnight's form."""
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        _area_id, camp_id = _enter_dungeon(db, client, 6, "Node040")
        client.startcamp(camp_id)
        client.event(camp_id, "conv_done")          # Wigwam
        client.startloc(camp_id, "NodeG")
        client.event(camp_id, "conv_done")          # Sister Midnight
        state = client.startloc(camp_id, "NodeE")   # Dream Bear
        bear = next(loc["Data"] for loc in state["VisLocs"]
                    if loc["Data"]["node"] == "NodeE")
        assert bear["type"] == "Convo", bear
        client.event(camp_id, "three_point_answer")
        state = client.event(camp_id, "conv_done")
        bear = next(loc["Data"] for loc in state["VisLocs"]
                    if loc["Data"]["node"] == "NodeE")
        scene = campaign.pve_db.db_encounter_scene_runtime(
            bear["encounter"], db)
        assert bear["type"] == "Encounter", bear
        assert scene[1] == "Tranquil Dream Bear Hard", scene
        _assert_client_contract(db, state)
        run = state["DungeonRun"]
        finale = campaign._drun_finale(run)
        stage = run["rooms"][finale]["stages"][0]
        forms = {}
        for total_points, expected in ((1, "Conquest"), (2, "Cunning"),
                                       (3, "Savagery")):
            run["answer_points"] = {str(i): total_points for i in range(5)}
            guid = campaign._drun_stage_encounter(run, finale, stage)
            forms[total_points] = campaign.pve_db.db_encounter_scene_runtime(
                guid, db)[1]
            assert forms[total_points].endswith(expected), forms
        run["answer_points"] = {"0": 1, "1": 2, "2": 1, "3": 2, "4": 1}
        guid = campaign._drun_stage_encounter(run, finale, stage)
        assert campaign.pve_db.db_encounter_scene_runtime(
            guid, db)[1].endswith("Retribution")
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_tranquil_dream_pays_by_win_order_and_boss():
    """Dungeon payouts follow the order of the run's wins, not the room;
    the finale pays the boss tier with the Tranquil Dream Pack."""
    from types import SimpleNamespace
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        _area_id, camp_id = _enter_dungeon(db, client, 6, "Node040")
        state = client.state(camp_id)
        rooms = {room["title"]: room for room in state["DungeonRun"]["rooms"]}
        order = ("Dream Spider", "Dream Eagle", "Dream Bear", "Dream Turtle",
                 "Dream Skarn")
        expected = ((75, 100), (125, 150), (250, 175), (250, 200), (300, 250))
        for index, (title, (gold, xp)) in enumerate(zip(order, expected)):
            scene = rooms[title]["stages"][0]["encounter"]
            session = SimpleNamespace(session_id=100 + index)
            records = campaign._dungeon_run_reward_records(
                state, scene, session)
            assert records == [{"gold": gold, "xp": xp,
                                "one_time": False}], (title, records)
            # The same game-end again keeps its tier.
            assert campaign._dungeon_run_reward_records(
                state, scene, session) == records
        boss = rooms["Sister Midnight"]["stages"][0]["encounter"]
        records = campaign._dungeon_run_reward_records(
            state, boss, SimpleNamespace(session_id=200))
        assert records == [{"gold": 450, "xp": 350, "one_time": False,
                            "chest_guid": "88d29ef0-5ec7-49b1-bcc5-8f70c5c4cda5"}], records
        # Every generic dungeon shares the tiers; its boss grants its pack.
        usurper = campaign._dungeon_reward_schedule("The Usurper")
        assert usurper["battles"][0] == {"gold": 75, "xp": 100}, usurper
        assert usurper["boss"]["chest_guid"] == \
            "eda1cb9f-9900-4218-a8d1-6564573a9cf2", usurper
        # Overworld battles keep their scene rewards.
        assert campaign._dungeon_run_reward_records(
            client.state(_area_id), boss, SimpleNamespace(session_id=1)) is None
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_az1_dungeons_play_through_the_generic_runner():
    """Every AZ1 dungeon the client can render runs end to end."""
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        finished, unavailable = [], {}
        _area_id, state = area_state(db)
        locs = {loc["Data"].get("node"): loc["Data"]
                for loc in state["VisLocs"]}
        for node in AZ1_DUNGEON_ENTRANCES:
            if locs[node]["type"] != "Dungeon":
                unavailable[node] = locs[node].get("dungeon_unavailable")
                continue
            client.pushes.clear()
            _area, camp_id = _enter_dungeon(db, client, 6, node)
            run, _played = _walk_dungeon(db, client, camp_id,
                                         lose_first=False)
            assert run["FinishReason"] == "Complete", node
            finished.append(run["DungeonRun"]["name"])
        assert {"The Tranquil Dream", "The Usurper", "The Smoldering Dead",
                "Devonshire Keep"} <= set(finished), finished
        # Fort Romor's template names nodes its client prefab does not have.
        assert "Node011" in unavailable, unavailable
        assert "missing from campaign/az01/fortromor" in \
            unavailable["Node011"], unavailable
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_start_event_on_a_dungeon_entrance_enters_it():
    """A save whose ALoc is still the entrance makes the map VM send
    ``start`` for the Dungeon location; that must enter the dungeon."""
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        area_id, state = area_state(db, 6)
        for loc in state["VisLocs"]:
            if loc["Data"].get("node") == "Node040":
                loc["Data"].update({"visible": True, "enabled": True})
                name = loc["Data"]["name"]
        state.update({"LastNode": "Node040", "ALoc": name})
        campaign._hydrate_az1_area_scene_metadata(
            db, state["VisLocs"], champ_id=6, state=state)
        db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                   (json.dumps(state), area_id))
        db.commit()
        client.event(area_id, "start")
        assert client.launched == [], client.launched
        push = next(args for args in client.pushes
                    if args[4] == "area_dungeon")
        assert push[5] == "DUNGEON" and push[6] is True
        assert client.state(area_id)["ALoc"] is None
    finally:
        client.restore()
        db.close()
        os.unlink(path)


def test_leaving_a_dungeon_returns_to_its_entrance():
    db, path = cloned_db()
    client = _DungeonClient(db)
    try:
        area_id, camp_id = _enter_dungeon(db, client, 6, "Node040")
        client.startcamp(camp_id)
        client.pushes.clear()
        campaign._handle_forfeit(client.handler, db, {"CampID": camp_id},
                                 *client._args())
        back = next(args for args in client.pushes
                    if args[4] == "dungeon_left")
        assert back[2] == area_id and back[5] == "AREA" and back[6] is True
        entrance = next(loc["Data"] for loc in client.state(area_id)["VisLocs"]
                        if loc["Data"]["node"] == "Node040")
        assert entrance["type"] == "Dungeon", entrance
        assert not entrance["completed"] and entrance["repeatable"], entrance
        # Entering again starts a fresh run in the same campaign row.
        client.pushes.clear()
        _area, again = _enter_dungeon(db, client, 6, "Node040")
        assert again == camp_id
        assert client.state(camp_id)["Finished"] is None
    finally:
        client.restore()
        db.close()
        os.unlink(path)


# Issue #34: the faction scenes behind Saramago Crater and Johannes Bog.
SPLIT_NODE_SCENES = {
    "Ardent": {"choice_Help": "NODE 23B - NECROTIC CHOICE HELP",
               "choice_NoHelp": "NODE 23B_2 - NECROTIC NO HELP",
               "Node026": "NODE 26B - WAYWARD VENNEN"},
    "Underworld": {"choice_Help": "NODE 23A - HUMANS CHOICE HELP",
                   "choice_NoHelp": "NODE 23A_2 - HUMANS NO HELP",
                   "Node026": "NODE 26 - WAYWARD ORCS"},
}
SPLIT_NODE_SUCCESS = {
    ("Ardent", "choice_Help"): "ARDENT Success - Player chose to help necrotic",
    ("Ardent", "choice_NoHelp"):
        "ARDENT Success - Player chose NOT to help necrotic",
    ("Underworld", "choice_Help"):
        "UNDERWORLD Success - Player chose to help humans",
    ("Underworld", "choice_NoHelp"):
        "UNDERWORLD Success - Player chose NOT to help humans",
}
SPLIT_VARIANTS = {"Node023A", "Node023B", "Node026A", "Node026B"}


def _scene_name(db, guid):
    row = campaign.pve_db.db_encounter_scene_runtime(guid, db)
    return str(row[1]) if row else None


def _split_node_location(state, node):
    return next(loc["Data"] for loc in state["VisLocs"]
                if loc["Data"]["node"] == node)


def _play_split_node(db, driver, camp_id, node, faction, choice=None):
    """Enter *node*, answer its intro, win its battle, close the Success."""
    import campaign_map_check as check
    names = _conversation_names()
    state = check._load_state(db, camp_id)
    data = _split_node_location(state, node)
    assert data["visible"] and data["enabled"], data
    driver.visit_node(camp_id, node)
    driver.startloc(camp_id, data["name"])
    state = check._load_state(db, camp_id)
    data = _split_node_location(state, node)
    assert state["ALoc"] == data["name"], state["ALoc"]
    assert data["type"] == "Convo" and data.get("pre_encounter"), data
    intro = names[data["conversationId"]]
    assert faction.lower() in intro.lower() and "success" not in \
        intro.lower(), intro
    scene_key = choice or node
    if choice:
        driver.event(camp_id, choice)
    driver.event(camp_id, "conv_done")
    state = check._load_state(db, camp_id)
    data = _split_node_location(state, node)
    assert data["type"] == "Encounter", data
    assert _scene_name(db, data["encounter"]).endswith(
        SPLIT_NODE_SCENES[faction][scene_key]), _scene_name(
            db, data["encounter"])
    assert data["encounter"] in {item["Name"] for item in state["Encounters"]}
    _response, pushes, _error = driver.event(camp_id, "start")
    assert any(push.get("RequestType") == "gamestarted" for push in pushes)
    assert check._load_state(db, camp_id)["ActiveEncounterGuid"] == \
        data["encounter"]
    campaign._apply_gameend(db, camp_id, True)
    state = check._load_state(db, camp_id)
    data = _split_node_location(state, node)
    success = names[data["conversationId"]]
    assert data["type"] == "Convo" and data["outcome_conversation"], data
    if choice:
        assert success.endswith(SPLIT_NODE_SUCCESS[(faction, choice)]), success
    else:
        assert success.lower().endswith(f"{faction.lower()} player success"), \
            success
    driver.event(camp_id, "conv_done")
    state = check._load_state(db, camp_id)
    data = _split_node_location(state, node)
    assert data["completed"] and state["ALoc"] is None, data
    assert not check.state_problems(db, state, "AZ1").get(node)
    return state


def test_saramago_crater_and_johannes_bog_play_by_faction_and_choice():
    """Issue #34: the faction-split nodes are one location on the client's
    map node; the faction picks the intro and battle, Saramago Crater's
    help / no-help answer picks its battle and Success conversation."""
    import campaign_map_check as check
    db, path = cloned_db()
    try:
        for champion_id, faction in ((6, "Ardent"), (4, "Underworld")):
            assert campaign._quest_faction_for_champion(
                db, champion_id) == faction
            area_id, state = area_state(db, champion_id)
            nodes = {loc["Data"]["node"] for loc in state["VisLocs"]}
            loc_nodes = {item["Name"] for item in state["LocNodes"]}
            assert {"Node023", "Node026"} <= nodes & loc_nodes
            assert not SPLIT_VARIANTS & (nodes | loc_nodes)
            # Brink Ridge's blockade is won; the player stands on it.
            brink = _split_node_location(state, "Node019")
            brink.update({"completed": True, "visible": True,
                          "enabled": True})
            pdata = state["PublicState"]["Data"]
            pdata["blockade_cleared"] = True
            pdata.setdefault("visited_nodes", []).append("Node019")
            state.update({"LastNode": "Node019", "ALoc": None})
            check._save_state(db, area_id, state)
            driver = check.Driver(db)
            driver.state_request(area_id)
            start = json.dumps(check._load_state(db, area_id))
            for choice in ("choice_Help", "choice_NoHelp"):
                db.execute("UPDATE campaigns SET state_json=? WHERE id=?",
                           (start, area_id))
                db.commit()
                state = _play_split_node(db, driver, area_id, "Node023",
                                         faction, choice)
                assert state["PublicState"]["Data"]["node_choices"] == {
                    "Node023": choice}
            # The route 023 -> 026 -> 027 is open.
            assert _split_node_location(state, "Node026")["visible"]
            state = _play_split_node(db, driver, area_id, "Node026", faction)
            assert _split_node_location(state, "Node027")["visible"]
    finally:
        db.close()
        os.unlink(path)


def test_stale_faction_variant_locations_move_to_their_map_node():
    """Issue #34: saves holding Node023A/B and Node026A/B are rewritten to
    the canonical nodes, keeping a won variant's result."""
    import campaign_map_check as check
    db, path = cloned_db()
    try:
        area_id, state = area_state(db, 6)
        stale_locs, stale_nodes = [], []
        for loc in state["VisLocs"]:
            data = loc["Data"]
            if data["node"] in ("Node023", "Node026"):
                for suffix in ("A", "B"):
                    stale_locs.append({"Data": dict(
                        data, node=data["node"] + suffix,
                        completed=(data["node"] + suffix == "Node023B"))})
            else:
                stale_locs.append(loc)
        for item in state["LocNodes"]:
            if item["Name"] in ("Node023", "Node026"):
                for suffix in ("A", "B"):
                    name = item["Name"] + suffix
                    stale_nodes.append({"Name": name, "Data": {
                        "id": name, "type": "DEFAULT"}})
            else:
                stale_nodes.append(item)
        pdata = state["PublicState"]["Data"]
        pdata["blocked_nodes"] = ["Node021", "Node023B", "Node023A",
                                  "Node023"]
        pdata["visited_nodes"] = pdata.get("visited_nodes", []) + ["Node023B"]
        pdata["conversation_visits"] = {"Node023B": 1}
        stale = dict(state, VisLocs=stale_locs, LocNodes=stale_nodes)
        stale_json = json.dumps(stale)

        def check_migrated(migrated):
            nodes = [loc["Data"]["node"] for loc in migrated["VisLocs"]]
            loc_nodes = [item["Name"] for item in migrated["LocNodes"]]
            assert not SPLIT_VARIANTS & set(nodes + loc_nodes), nodes
            assert nodes.count("Node023") == 1 == nodes.count("Node026")
            assert loc_nodes.count("Node023") == 1 == loc_nodes.count(
                "Node026")
            assert _split_node_location(migrated, "Node023")["completed"]
            data = migrated["PublicState"]["Data"]
            assert data["blocked_nodes"].count("Node023") == 1
            assert "Node023" in data["visited_nodes"]
            assert data["conversation_visits"]["Node023"] == 1

        # A reconnect (getcampstate hydration) repairs the save in place.
        check._save_state(db, area_id, json.loads(stale_json))
        check.Driver(db).state_request(area_id)
        check_migrated(check._load_state(db, area_id))
        # Re-entering the map from a panorama rebuilds it from SceneData and
        # must keep the stale save's progress.
        check._save_state(db, area_id, json.loads(stale_json))
        _cid, rebuilt = campaign._activate_az1_area(db, 6)
        check_migrated(rebuilt)
    finally:
        db.close()
        os.unlink(path)


# Map-wide check (issue #31, part C).  Known problems the check reports on a
# fresh database; each entry is a real, unfixed finding with its reason.  A
# fix must remove its entry (the comparison is exact).  Saramago Crater and
# Johannes Bog (Node023/Node026) were fixed by issue #34.
KNOWN_AZ1_MAP_PROBLEMS = {}
# SceneData locations without a client map node: never selectable, so not
# visited (they must stay hidden; a visible one is a map problem).
AZ1_NON_MAP_LOCATIONS = {"Node019B", "019B1", "Node019B2", "019B3", "Node019C"}
# AZ2 has no AREA runtime yet; its data check may report only this.
AZ2_DATA_TODO = {node: ["dungeon terrain without a DungeonTemplate entrance "
                        "scene"]
                 for node in ("Node002", "Node019", "Node040", "Node044")}


def test_every_az1_node_keeps_the_client_contract():
    """Issue #31 C: every AZ1 node passes the client contract when the map
    is loaded, visited fresh and revisited after it was resolved."""
    import campaign_map_check as check
    db, path = cloned_db()
    try:
        area_id, state = area_state(db)
        problems, served = check.check_area_state(db, area_id)
        assert problems == KNOWN_AZ1_MAP_PROBLEMS, problems
        # Deck-less scenes are never offered to GetChampionTemplate: the
        # Tanning Dog (zero-GUID deck) is a conversation, and panorama scenes
        # stay out of the Encounters catalog.
        locs = {data["node"]: data for data in check._locations(served)}
        assert locs["Node046"]["type"] == "Convo", locs["Node046"]
        catalog = {item["Name"] for item in served["Encounters"]}
        for node in ("Node017", "Node034", "Node049"):
            assert locs[node]["type"] == "Panorama", locs[node]
            assert locs[node]["encounter"] not in catalog, locs[node]
        hidden = {node for node in AZ1_NON_MAP_LOCATIONS
                  if not locs[node]["visible"]}
        assert hidden == AZ1_NON_MAP_LOCATIONS, hidden

        reports, skipped = check.simulate_area(db, area_id)
        assert set(skipped) == AZ1_NON_MAP_LOCATIONS, skipped
        prefab = set(check.client_map_nodes(db)) - set(KNOWN_AZ1_MAP_PROBLEMS)
        prefab -= campaign._az1_path_fork_ids(db)
        assert {r.node for r in reports} == prefab, len(reports)
        bad = [r.line() for r in reports if not r.ok]
        assert not bad, "\n".join(bad)
        notes = {r.node: " ".join(r.notes) for r in reports}
        # Gated nodes were opened (forced, or through the story), not skipped.
        assert "forced open (hidden, quest-blocked)" in notes["Node021"]
        for node in AZ1_DUNGEON_ENTRANCES:
            if node != "Node011":  # Fort Romor: the safe Empty fallback
                assert "area_dungeon -> DUNGEON" in notes[node], notes[node]
        assert "area_panorama -> PANORAMA" in notes["Node017"]

        assert not check.AREA_MAPS["AZ2"]["runtime"]
        assert check.static_area_problems(db, "AZ2") == AZ2_DATA_TODO
    finally:
        db.close()
        os.unlink(path)


def test_every_dungeon_map_keeps_the_client_contract():
    """Issue #31 C: every runnable DungeonTemplate plays to its end with
    the client contract kept at every step; Crayburn's fresh state too."""
    import campaign_map_check as check
    db, path = cloned_db()
    try:
        for champion_id in (4, 6, 7):
            assert check.check_crayburn(db, champion_id) == {}, champion_id
        results = {name: (status, problems) for name, status, problems in
                   check.check_dungeons(db, 6)}
        for name in ("The Tranquil Dream", "The Usurper",
                     "The Smoldering Dead", "Devonshire Keep"):
            assert results[name] == ("finished", {}), (name, results[name])
        for name in ("Fort Romor", "Castle Crayburn"):
            assert results[name][0].startswith("known: "), results[name]
        # AZ2 dungeons have no overworld entrance yet but already run.
        for name in ("Brutecrown Bluff", "Great Machine Graveyard",
                     "Ruins of Kukatan"):
            assert results[name] == (
                "finished (no overworld entrance yet)", {}), results[name]
        # The Kraken's boat battles name a deck but no AI champion.
        status, problems = results["The Kraken"]
        assert status == "finished (no overworld entrance yet)", status
        assert problems and all("has no AI champion" in item
                                for items in problems.values()
                                for item in items), problems
        assert not [name for name, (status, _p) in results.items()
                    if status.startswith("unavailable")], results
    finally:
        db.close()
        os.unlink(path)


if __name__ == "__main__":
    tests = [
        test_cross_zila_objective_is_linked_to_savage_lord,
        test_node00r_uses_authored_node_r_conversation_fallback,
        test_node00r_faction_quest_branch_opens_lena_grotto_and_stays_retryable,
        test_node00r_finished_faction_quest_uses_terminal_conversation,
        test_cross_zila_and_zodiac_gates_follow_quest_progress,
        test_winston_step1_starts_cross_the_river,
        test_area_gameend_keeps_the_hero_on_the_encounter_node,
        test_area_state_repair_drops_az0_trainer_leftovers,
        test_startloc_on_the_already_active_location_keeps_it_active,
        test_savage_lord_defeat_conversation_keeps_the_encounter_open,
        test_failed_and_completed_encounters_are_reopened,
        test_visited_winston_backfills_the_missing_cross_the_river_quest,
        test_az1_unfinished_encounter_does_not_reveal_outgoing_paths,
        test_az1_unvisited_repeatable_conversations_are_not_blue,
        test_az1_direct_node00r_route_is_not_replaced_by_fork_path,
        test_az1_alternate_reveal_does_not_unlock_incomplete_path,
        test_az1_conv_done_recomputes_repeatable_node_paths,
        test_cross_zila_turnin_refreshes_bridge_path_and_weston_conversation,
        test_savage_lord_advances_cross_zila_and_node_rewards_are_authored,
        test_corrupt_dryad_encounter_grants_pack_and_faction_equipment,
        test_az1_scene_lookup_keeps_authored_node_variants_distinct,
        test_az1_objectives_use_faction_specific_conversations,
        test_az1_opening_uses_the_underworld_route_and_fog_gates,
        test_az1_panorama_scene_transitions_out_of_area_on_startloc,
        test_az1_panorama_uses_authored_repeat_hint_when_taming_is_in_progress,
        test_az1_panorama_conversations_stay_visible_and_grant_new_quests,
        test_az1_panorama_handoff_retries_on_arrived_node_without_encounter_field,
        test_az1_panorama_return_parent_resumes_area_progress,
        test_az1_travel_button_transitions_completed_cave_in_on_visit_node,
        test_az1_multi_edge_travel_path_transitions_completed_cave_in,
        test_az1_repeated_path_from_cave_in_does_not_reopen_panorama,
        test_crayburn_defeat_conversation_keeps_encounter_retryable,
        test_crayburn_locked_future_node_does_not_replace_tower_gate_start,
        test_dungeon_win_count_tracks_streak_for_pregame_talents,
        test_post_crayburn_report_opens_the_az1_transition,
        test_tamed_capture_pushes_the_quest_progress,
        test_campaign_game_end_cleans_up_once,
        test_fortune_of_blood_resolves_both_authored_abilities,
        test_only_closing_quest_conversations_are_turn_ins,
        test_unlocked_west_bridge_visit_survives_the_tamed_gate,
        test_bubblebee_objective_points_at_its_capture_scene,
        test_won_capture_node_reopens_while_its_objective_is_pending,
        test_indigo_river_prelude_is_a_conversation_not_a_battle,
        test_finished_cross_zodiac_keeps_the_west_bridge_open,
        test_wallace_follows_the_story_order_on_revisits,
        test_warren_variant_follows_the_dryad_outcome,
        test_dungeon_entrances_are_never_deckless_battles,
        test_tranquil_dream_entrance_starts_its_dungeon,
        test_tranquil_dream_run_completes_its_quest_objective,
        test_tranquil_dream_pays_by_win_order_and_boss,
        test_tranquil_dream_answers_pick_the_battle_and_boss_form,
        test_az1_dungeons_play_through_the_generic_runner,
        test_start_event_on_a_dungeon_entrance_enters_it,
        test_leaving_a_dungeon_returns_to_its_entrance,
        test_saramago_crater_and_johannes_bog_play_by_faction_and_choice,
        test_stale_faction_variant_locations_move_to_their_map_node,
        test_every_az1_node_keeps_the_client_contract,
        test_every_dungeon_map_keeps_the_client_contract,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
