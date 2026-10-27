"""Store / Escrow service handlers: GetStoreItems, PurchaseItem, RedeemCode."""

import json, gzip, os

from db import log_req
from profile_db import (db_get_store_items, db_adjust_user_currency,
                        db_record_purchase, db_add_inventory, db_save_deck,
                        db_redeem_code, db_send_email,
                        db_next_card_instance_for_user, db_insert_card_instance,
                        db_add_collection, db_get_store_item,
                        db_get_user_currency, db_set_user_currency,
                        db_set_inventory_client_uid)
from pvp_db import db_pvp_booster_card_guids
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper, encode_store_response

# Store deck data — loaded from JSON files in Hex root
_ROOT = os.path.dirname(os.path.dirname(__file__))

_STORE_DECK_CARDS = {}
_path = os.path.join(_ROOT, "starter_deck_cards.json")
if os.path.exists(_path):
    with open(_path) as f:
        _STORE_DECK_CARDS = json.load(f)

_STARTER_DECKS = {}
_path2 = os.path.join(_ROOT, "generated", "starter_decks.json")
if os.path.exists(_path2):
    with open(_path2) as f:
        _STARTER_DECKS = json.load(f)


def send_purchase_response(handler, target, instance, reqid, comp,
                           session_id, conh, service_uid, *, remaining,
                           currency_type, granted_list=(), error_code=0,
                           error_message=""):
    """Send the stock-client PurchaseItem response, including its result."""
    inventory_type = "System.Collections.Generic.List`1#Game.Shared.Domain.inventory_bits"
    deck_type = "System.Collections.Generic.List`1#Game.Shared.Domain.deck_bits"
    card_type = "System.Collections.Generic.List`1#Game.Shared.Domain.card_instance_bits"
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Escrow.PurchaseItemResponse",
         "System.Int32", "System.String", inventory_type,
         "Game.Shared.Domain.inventory_bits", "Game.Shared.ResourceId",
         "System.Guid", "System.DateTime", deck_type, card_type,
         inventory_type, deck_type, card_type, inventory_type, card_type,
         inventory_type, "Game.Shared.Network.Escrow.EPurchaseItemError",
         "System.String"],
        [("RemainingCurrency", "int", int(remaining or 0)),
         ("TransactionCurrencyType", "string", currency_type),
         ("PurchasedInventory", "coll", (inventory_type, 0)),
         ("PurchasedDeckBits", "coll", (deck_type, 0)),
         ("PurchasedCards", "coll", (card_type, 0)),
         ("GrantedInventory", "coll",
          (inventory_type, len(granted_list), list(granted_list))),
         ("GrantedDeckBits", "coll", (deck_type, 0)),
         ("GrantedCards", "coll", (card_type, 0)),
         ("ConsumedInventory", "coll", (inventory_type, 0)),
         ("ConsumedCards", "coll", (card_type, 0)),
         ("CurrencyInventory", "coll", (inventory_type, 0)),
         ("Error", "enum1",
          ("Game.Shared.Network.Escrow.EPurchaseItemError", int(error_code))),
         ("ErrorMessage", "string", str(error_message or ""))]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6011, resp_body, comp, session_id)
    issuer_str = (f"0.0.0.0.ServiceEscrow.{service_uid}.ServicePlayer."
                  f"{handler.client_uid}.{resp_reqid}")
    handler.scnt += 1
    handler.send_and_cache({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes, 6011, reqid, target, instance)


def send_purchase_failure(handler, target, instance, reqid, comp,
                          session_id, conh, service_uid, item_id,
                          error_message="Purchase failed."):
    """Reply to a failed service dispatch without retrying a legacy mutation."""
    p = handler.user_profile if isinstance(handler.user_profile, dict) else {}
    user_id = p.get("id")
    try:
        row = db_get_store_item(int(item_id))
        currency_type = row[2] if row else "Gold"
    except (TypeError, ValueError, IndexError):
        currency_type = "Gold"
    currency_column = "platinum" if currency_type == "Platinum" else "gold"
    try:
        remaining = db_get_user_currency(user_id, currency_column) \
            if user_id is not None else p.get(currency_column, 0)
    except Exception:
        remaining = p.get(currency_column, 0)
    send_purchase_response(
        handler, target, instance, reqid, comp, session_id, conh, service_uid,
        remaining=remaining, currency_type=currency_type, error_code=6,
        error_message=error_message)


def _grant_deck_to_player(user_id, cards, deck_name, handler=None, conn=None):
    """Grant cards to collection + card_instances, create deck, push to client.
    
    The deck's cards column stores instance IDs (integer), not template GUIDs,
    so the client's deck loader can resolve all 23 fields of card_instance_bits.
    """
    connection = conn
    cid = db_next_card_instance_for_user(user_id, conn=connection)
    instance_ids = []
    for card_guid, count in cards:
        db_add_collection(user_id, card_guid, count, conn=connection)
        for _ in range(count):
            db_insert_card_instance(user_id, cid, card_guid, conn=connection)
            instance_ids.append(cid)
            cid += 1
    cards_json = json.dumps(instance_ids)
    deck_db_id = db_save_deck(user_id, deck_name, cards_json, conn=conn)
    log_req(f"    Granted {deck_name}: {len(instance_ids)} cards, deck_id={deck_db_id}")
    if handler is not None:
        handler.push_cards_to_client()
    return deck_db_id


def grant_vendor_code_cards(user_id, redeem_code, conn=None):
    """Grant card instances for a card-bearing vendor code."""
    if str(redeem_code).strip().lower() != "allpvp":
        return []
    next_id = db_next_card_instance_for_user(user_id, conn=conn)
    grants = []
    for template_guid in db_pvp_booster_card_guids(conn=conn):
        db_add_collection(user_id, template_guid, 4, conn=conn)
        for _ in range(4):
            db_insert_card_instance(user_id, next_id, template_guid,
                                    conn=conn)
            next_id += 1
        grants.append((template_guid, 4))
    log_req(f"    Granted allpvp: {len(grants)} templates x4")
    return grants


def apply_purchase(conn, user_id, item_id, quantity):
    """Apply an affordable store purchase in the caller-owned transaction."""
    row = db_get_store_item(item_id, conn=conn)
    if not row:
        balance = db_get_user_currency(user_id, "gold", conn=conn)
        return {
            "success": False, "error_code": 6,
            "error_message": "Unknown store item.",
            "remaining": int(balance or 0), "currency": "Gold",
            "item_name": "Unknown", "template_guid": "",
            "quantity": 0, "item_id": int(item_id),
            "deck_granted": False, "granted_list": [], "total_cost": 0,
        }
    item_name, cost, currency_type, template_guid, store_tab = row
    quantity = int(quantity)
    balance_column = "platinum" if currency_type == "Platinum" else "gold"
    balance = int(db_get_user_currency(
        user_id, balance_column, conn=conn) or 0)
    cost = int(cost or 0)

    def rejected(error_code, message):
        return {
            "success": False, "error_code": error_code,
            "error_message": message, "remaining": balance,
            "currency": currency_type, "item_name": item_name,
            "template_guid": template_guid, "quantity": quantity,
            "item_id": int(item_id), "deck_granted": False,
            "granted_list": [], "total_cost": 0,
        }

    if quantity <= 0 or cost < 0:
        return rejected(6, "Invalid purchase quantity or price.")
    total_cost = cost * quantity
    if total_cost > balance:
        error_code = (1 if currency_type == "Platinum" else
                      2 if currency_type == "Gold" else 3)
        result = rejected(error_code, "Insufficient funds.")
        result["total_cost"] = total_cost
        return result

    remaining = balance - total_cost
    db_set_user_currency(user_id, balance_column, remaining, conn=conn)

    granted_list = []
    deck_granted = False
    if template_guid:
        granted_list = [(template_guid, 1000 + int(item_id), int(quantity))]
    if store_tab == "collectordeck" and template_guid:
        deck_data = _STORE_DECK_CARDS.get(template_guid)
        if deck_data:
            _grant_deck_to_player(
                user_id, deck_data["cards"], deck_data.get("name", item_name),
                conn=conn)
            deck_granted = True
        elif item_name:
            race = item_name.replace(" Starter Deck", "").replace(
                " Starter", "").strip()
            if race in _STARTER_DECKS:
                _grant_deck_to_player(
                    user_id, _STARTER_DECKS[race]["cards"],
                    f"{race} Starter Deck", conn=conn)
                deck_granted = True
    db_record_purchase(
        user_id, item_name, template_guid, int(cost) * int(quantity),
        currency_type, conn=conn)
    if template_guid:
        db_add_inventory(user_id, template_guid, int(quantity), conn=conn)
        db_set_inventory_client_uid(
            user_id, template_guid, 1000 + int(item_id), conn=conn)
    return {
        "success": True,
        "error_code": 0,
        "error_message": "",
        "remaining": remaining,
        "currency": currency_type,
        "item_name": item_name,
        "template_guid": template_guid,
        "quantity": int(quantity),
        "item_id": int(item_id),
        "deck_granted": deck_granted,
        "granted_list": granted_list,
        "total_cost": total_cost,
    }


def apply_redeem(conn, user_id, redeem_code):
    """Apply a redemption and its system email in one transaction."""
    from services import redeem_bundles
    if redeem_bundles.is_bundle_code(redeem_code):
        return _apply_bundle_redeem(conn, user_id, redeem_code)
    result = db_redeem_code(redeem_code, conn=conn)
    if not result["redeemed"]:
        return {"gold": 0, "platinum": 0, "redeemed": False,
                "error_message": result["error_message"]}
    card_grants = grant_vendor_code_cards(user_id, redeem_code, conn=conn)
    new_gold, new_platinum = db_adjust_user_currency(
        user_id, gold_delta=result["gold"],
        platinum_delta=result["platinum"], conn=conn)
    parts = []
    if result["gold"] > 0:
        parts.append(f"{result['gold']:,} Gold")
    if result["platinum"] > 0:
        parts.append(f"{result['platinum']:,} Platinum")
    if card_grants:
        parts.append(f"{len(card_grants)} PvP card templates x4")
    reward_desc = ", ".join(parts)
    db_send_email(
        user_id, f"Code Redeemed: {redeem_code}",
        f"You have successfully redeemed code '{redeem_code}' and "
        f"received {reward_desc}.\n\nThank you for playing Hex: Shards of Fate!",
        "SYSTEM", conn=conn)
    return {
        "gold": result["gold"], "platinum": result["platinum"],
        "card_grants": card_grants,
        "redeemed": True,
        "error_message": "",
    }


def _apply_bundle_redeem(conn, user_id, redeem_code):
    """Grant a redeem_bundles.json bundle once per player."""
    from services import redeem_bundles
    code = redeem_bundles.code_key(redeem_code)
    if not redeem_bundles.claim(conn, user_id, code):
        return {"gold": 0, "platinum": 0, "redeemed": False,
                "error_message": "You have already redeemed this code."}
    granted = redeem_bundles.apply_bundle(conn, user_id, code)
    contents = "\n".join(f"- {line}" for line in granted["summary"]) or "- a thank-you"
    db_send_email(
        user_id, f"Code Redeemed: {granted['title']}",
        f"You have successfully redeemed code '{code}' and received:\n"
        f"{contents}\n\nThank you for playing Hex: Shards of Fate!",
        "SYSTEM", conn=conn)
    log_req(f"    Bundle {code}: {granted['card_copies']} cards, "
            f"{len(granted['inventory_updates'])} inventory items, "
            f"decks={granted['decks']}")
    return {"gold": 0, "platinum": 0, "redeemed": True, "error_message": "",
            "card_grants": granted["card_copies"] or granted["decks"],
            "inventory_updates": granted["inventory_updates"],
            "reward_template_ids": granted["reward_template_ids"]}


def handle_get_items(handler, target, instance, reqid, comp, session_id, conh,
                     SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — GetStoreItems (6009)."""
    items = db_get_store_items()
    log_req(f">>> Respond GetStoreItems -> {len(items)} items")
    resp_inner = encode_store_response(items)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6009, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent GetStoreItems response ({len(dw_bytes)}b)")


def handle_purchase(handler, target, instance, reqid, comp, session_id, conh,
                    inner_obj, SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — PurchaseItem (6011)."""
    quantity = inner_obj.get("Quanity", 1)
    item_id = inner_obj.get("Id", 1)
    log_req(f">>> PurchaseItem: qty={quantity} id={item_id}")

    from application.commands import PurchaseStoreItemCommand
    result = handler._application.execute(PurchaseStoreItemCommand(
        user_id=handler.user_profile["id"],
        item_id=int(item_id),
        quantity=int(quantity),
    )).value
    item_name = result["item_name"]
    currency_type = result["currency"]
    template_guid = result["template_guid"]
    remaining = result["remaining"]
    granted_list = result["granted_list"]
    success = bool(result.get("success", True))
    p = handler.user_profile
    if currency_type == "Platinum":
        p["platinum"] = remaining
    else:
        p["gold"] = remaining

    send_purchase_response(
        handler, target, instance, reqid, comp, session_id, conh,
        SERVICE_MAIL_UID, remaining=remaining,
        currency_type=currency_type, granted_list=granted_list,
        error_code=result.get("error_code", 0),
        error_message=result.get("error_message", ""))
    if success and result["deck_granted"]:
        handler.push_cards_to_client()
    if success and template_guid:
        handler.push_inventory_to_client(
            qty=result["quantity"], template_guid=template_guid,
            item_id=1000 + result["item_id"])
    if success:
        log_req(f"    Sent PurchaseItem response: remaining={remaining} {currency_type}")
    else:
        log_req(
            f"    Rejected PurchaseItem: {result['error_message']} "
            f"balance={remaining} {currency_type} total="
            f"{result.get('total_cost', 0)}")


def encode_redeem_response(item_template_ids, gold_delta, plat_delta,
                           error_value, error_message):
    """RedeemCodeResponse; ItemTemplateIds fill the client's reward window."""
    from objfmt_builder import ObjFmtBuilder
    b = ObjFmtBuilder("Game.Client.Network.Escrow.RedeemCodeResponse")
    b.field_resource_id_list("ItemTemplateIds", list(item_template_ids))
    b.field_int("GoldDelta", gold_delta)
    b.field_int("PlatinumDelta", plat_delta)
    for name, element in (("CardBits", "card_instance_bits"),
                          ("StarterDecksBits", "deck_bits")):
        list_idx, start = b.begin_list(
            name, f"System.Collections.Generic.List`1#Game.Shared.Domain.{element}", 0)
        b._set_size(list_idx, start)
    b.field_enum("Error", "Game.Shared.Network.Escrow.ERedeemCodeError",
                 error_value)
    b.field_str("ErrorMessage", error_message)
    return b.finish(7)


# Larger grants (e.g. every piece of equipment) reach the client at relog.
_MAX_INVENTORY_PUSHES = 200


def handle_redeem(handler, target, instance, reqid, comp, session_id, conh,
                  inner_obj, SERVICE_MAIL_UID, **_kw):
    """ServiceEscrow — RedeemCode (6013)."""
    redeem_code = inner_obj.get("RedeemCode", "")
    log_req(f">>> RedeemCode: code='{redeem_code}'")

    p = handler.user_profile
    from application.commands import RedeemCodeCommand
    result = handler._application.execute(RedeemCodeCommand(
        user_id=p["id"], code=redeem_code)).value
    gold_delta = result["gold"]
    plat_delta = result["platinum"]
    if result["redeemed"]:
        p["gold"] += gold_delta
        p["platinum"] += plat_delta
        if result.get("card_grants"):
            handler.push_cards_to_client()
        updates = result.get("inventory_updates") or ()
        if len(updates) <= _MAX_INVENTORY_PUSHES:
            for template_guid, item_uid, total in updates:
                handler.push_inventory_to_client(
                    qty=total, template_guid=template_guid, item_id=item_uid)
        else:
            log_req(f"    {len(updates)} inventory items arrive on next login")
        log_req(f"    RedeemCode success: gold+{gold_delta} plat+{plat_delta}")
    else:
        log_req(f"    RedeemCode invalid: {redeem_code}")
    error_value = 0 if result["redeemed"] else 1
    error_message = result.get("error_message", "")
    resp_inner = encode_redeem_response(
        result.get("reward_template_ids") or (), gold_delta, plat_delta,
        error_value, error_message)

    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, 6013, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceEscrow.{SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send_and_cache({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes, 6013, reqid, target, instance)
    log_req(f"    Sent RedeemCode response ({len(dw_bytes)}b)")
