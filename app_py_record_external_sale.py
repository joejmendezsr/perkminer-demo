# =====================================================================================
# Replacement for record_external_sale() in app.py   (the ecommerce_verify_beacon route can stay as it is)
#
# What changed vs. your version:
#   1. CORS: the "thank you" page lives on the BUSINESS's website, so the browser sends a cross-site request
#      with a JSON body -> it first sends an OPTIONS "preflight". Your old route only allowed POST and sent no
#      CORS headers, so browsers blocked the call (the script's fetch would fail even though the route is fine).
#   2. Validation: float("nan") / float("inf") / huge numbers used to pass "> 0". Non-numbers (lists, objects)
#      crashed with a 500. Now: finite, > 0, <= 1,000,000, rounded to cents.
#   3. The response includes "receipt_url" (built from your real show_user_receipt route), so the script
#      doesn't have to guess the receipt address.
#
# Needs at the top of app.py:   import math      (and make_response is imported from flask)
# =====================================================================================

def _ext_json(payload, status=200):
    resp = make_response(jsonify(payload), status)
    resp.headers["Access-Control-Allow-Origin"] = "*"      # no cookies are used, the signed token is the credential
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


@csrf.exempt
@app.route("/api/record_external_sale", methods=["POST", "OPTIONS"])
def record_external_sale():
    if request.method == "OPTIONS":                 # browser preflight
        return _ext_json({}, 204)

    data = request.get_json(silent=True) or {}
    token = data.get("perk_token")
    amount = data.get("amount")

    if not token or amount is None:
        return _ext_json({"error": "missing token or amount"}, 400)

    # 1) verify token
    try:
        payload = shop_serializer.loads(token)
    except Exception:
        return _ext_json({"error": "invalid token"}, 400)

    interaction_id = payload.get("interaction_id")
    user_id = payload.get("user_id")
    business_id = payload.get("business_id")

    interaction = Interaction.query.get(interaction_id)
    if (
        not interaction or
        interaction.user_id != user_id or
        interaction.business_id != business_id or
        interaction.status != "active"
    ):
        return _ext_json({"error": "invalid or inactive interaction"}, 400)

    business = Business.query.get(business_id)
    if not business:
        return _ext_json({"error": "business not found"}, 400)

    # 2) validate amount
    try:
        amount_float = round(float(amount), 2)
        if not math.isfinite(amount_float) or amount_float <= 0 or amount_float > 1_000_000:
            raise ValueError()
    except (ValueError, TypeError):
        return _ext_json({"error": "invalid amount"}, 400)

    # 3) your existing finalize_interaction logic
    try:
        summary = finalize_interaction(
            interaction=interaction,
            business=business,
            amount=amount_float,
            staff_id=None,
            source="external_cart",
            local_date_time=None
        )
    except Exception as e:
        db.session.rollback()
        app.logger.warning("external sale failed for interaction %s: %s", interaction_id, e)
        # e.g. insufficient funds in account_balance, etc.
        return _ext_json({"error": str(e)}, 400)

    # 4) end the session
    interaction.status = "ended"
    interaction.awaiting_payment = False
    db.session.commit()

    return _ext_json({
        "status": "ok",
        "summary": summary,
        "receipt_url": url_for("show_user_receipt", interaction_id=interaction.id, _external=True),
    }, 200)
