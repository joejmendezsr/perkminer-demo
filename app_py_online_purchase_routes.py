# =====================================================================================================
# ONLINE PURCHASE ROUTES  -  REPLACE everything you have for online purchases with this whole file:
#     delete:  _ext_json, record_external_sale, ecommerce_verify_beacon, online_purchase_instructions,
#              admin_ecommerce_verifications
#     paste :  everything below (anywhere after finalize_interaction / the models are defined)
#
# ALSO DO THESE (one time):
#   1. Run migration_external_sales.sql in pgAdmin.
#   2. Add the two columns to class Business (next to ecommerce_verified):
#          sales_key = db.Column(db.String(64))
#          shopify_webhook_secret = db.Column(db.String(160))
#   3. Add the ExternalSale model (right below this comment block is fine, or next to the other models).
#   4. Line ~210:  shop_serializer = URLSafeTimedSerializer(SHOP_SECRET, salt="shop-redirect")
#   5. Make sure app.py has:   import base64, hmac, hashlib, time, math, secrets   (all but base64 already exist)
#
# HOW THE AMOUNT IS PROTECTED
#   The member's browser can no longer tell us how much was spent. The sale amount only counts when it
#   arrives from the business's own SERVER (or Shopify's server), signed with a secret key that only the
#   business has:
#     * /api/external_sale/confirm          - signed by the business's server (custom sites, WooCommerce)
#     * /api/external_sale/shopify/<id>     - Shopify's signed "Order payment" webhook
#     * /api/record_external_sale           - browser helper: it ONLY asks "was my sale recorded yet?"
#                                             and returns the receipt link. Any amount sent to it is ignored.
# =====================================================================================================


class ExternalSale(db.Model):
    """One row per online order we recorded. The unique (business, order) pair makes retries harmless."""
    __tablename__ = "external_sales"
    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("business.id"), nullable=False, index=True)
    order_ref = db.Column(db.String(120), nullable=False)
    interaction_id = db.Column(db.Integer)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    source = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("business_id", "order_ref", name="uq_external_sale_order"),)


SHOP_TOKEN_MAX_AGE = 60 * 60 * 24 * 2      # a shop token is good for 48 hours
SIGNATURE_MAX_SKEW = 300                   # signed requests must be less than 5 minutes old


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------
def _ext_json(payload, status=200):
    """JSON reply that browsers on OTHER websites may read (only used by the receipt-status helper)."""
    resp = make_response(jsonify(payload), status)
    resp.headers["Access-Control-Allow-Origin"] = "*"      # no cookies are used, the signed token is the credential
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def _get_or_create_sales_key(biz):
    if not biz.sales_key:
        biz.sales_key = secrets.token_urlsafe(32)
        db.session.commit()
    return biz.sales_key


def _verify_signed_request():
    """Checks X-PerkMiner-Business / -Timestamp / -Signature.
    Signature = hex HMAC-SHA256(sales_key, "<timestamp>." + raw request body).
    Returns (business, raw_body, None) when valid, or (None, None, (json, status)) when not."""
    raw = request.get_data()
    try:
        biz_id = int(request.headers.get("X-PerkMiner-Business", ""))
        ts = request.headers.get("X-PerkMiner-Timestamp", "")
        age = abs(time.time() - int(ts))
    except (TypeError, ValueError):
        return None, None, ({"error": "missing or invalid signature headers"}, 400)
    sig = (request.headers.get("X-PerkMiner-Signature", "") or "").strip().lower()

    biz = Business.query.get(biz_id)
    key = biz.sales_key if biz else None
    if not key:
        return None, None, ({"error": "unauthorized"}, 401)
    if age > SIGNATURE_MAX_SKEW:
        return None, None, ({"error": "timestamp too old - check your server clock"}, 401)
    expected = hmac.new(key.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None, None, ({"error": "bad signature"}, 401)
    return biz, raw, None


def _log_ping(biz, kind):
    db.session.add(EcommerceVerificationPing(
        business_id=biz.id,
        user_agent=(kind + " | " + (request.headers.get("User-Agent") or ""))[:500],
        ip_address=request.remote_addr,
    ))
    db.session.commit()


def _receipt_url(interaction_id):
    return url_for("show_user_receipt", interaction_id=interaction_id, _external=True)


def _complete_external_sale(biz, token, amount, order_ref, source):
    """The ONE place an online sale gets recorded. Returns (payload_dict, http_status).
    Safe to call twice for the same order (returns the first result, never double-pays)."""
    # 1) the order must not have been recorded already
    existing = ExternalSale.query.filter_by(business_id=biz.id, order_ref=order_ref).first()
    if existing:
        return {"status": "ok", "duplicate": True, "receipt_url": _receipt_url(existing.interaction_id)}, 200

    # 2) the shop token must be genuine, unexpired and belong to THIS business
    try:
        payload = shop_serializer.loads(token, max_age=SHOP_TOKEN_MAX_AGE)
    except Exception:
        return {"error": "invalid or expired perk_token"}, 400
    if payload.get("business_id") != biz.id:
        return {"error": "perk_token belongs to a different business"}, 400

    # 3) amount (comes from the business's server, never from the member's browser)
    try:
        amount_f = round(float(amount), 2)
        if not math.isfinite(amount_f) or amount_f <= 0 or amount_f > 1_000_000:
            raise ValueError()
    except (TypeError, ValueError):
        return {"error": "invalid amount"}, 400

    # 4) lock the session row so simultaneous requests cannot both finalize it
    interaction = Interaction.query.filter_by(id=payload.get("interaction_id")).with_for_update().first()
    if (not interaction or interaction.user_id != payload.get("user_id")
            or interaction.business_id != biz.id):
        db.session.rollback()
        return {"error": "invalid session"}, 400
    if (interaction.status != "active"
            or BusinessTransaction.query.filter_by(interaction_id=interaction.id).first()):
        # (the BusinessTransaction check also covers a request that was waiting on the row lock above)
        db.session.rollback()
        return {"error": "this member session already has a recorded sale"}, 409

    try:
        finalize_interaction(interaction=interaction, business=biz, amount=amount_f,
                             staff_id=None, source=source, local_date_time=None)
        db.session.add(ExternalSale(business_id=biz.id, order_ref=order_ref, interaction_id=interaction.id,
                                    amount=amount_f, source=source))
        interaction.status = "ended"
        interaction.awaiting_payment = False
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        again = ExternalSale.query.filter_by(business_id=biz.id, order_ref=order_ref).first()
        if again:
            return {"status": "ok", "duplicate": True, "receipt_url": _receipt_url(again.interaction_id)}, 200
        return {"error": "could not record sale"}, 400
    except Exception as e:
        db.session.rollback()
        app.logger.warning("external sale failed (business %s, order %s): %s", biz.id, order_ref, e)
        if "Insufficient funds" in str(e):
            return {"error": "insufficient_balance",
                    "message": "Add funds to your PerkMiner advertising balance, then retry."}, 402
        if "already been finalized" in str(e):
            return {"error": "this member session already has a recorded sale"}, 409
        return {"error": "could not record sale"}, 400

    return {"status": "ok", "receipt_url": _receipt_url(interaction.id)}, 200


# ---------------------------------------------------------------------------------------------------
# 1) Business server -> PerkMiner (custom websites, WooCommerce, anything with server code)
# ---------------------------------------------------------------------------------------------------
@csrf.exempt
@app.route("/api/external_sale/confirm", methods=["POST"])
def external_sale_confirm():
    biz, raw, err = _verify_signed_request()
    if err:
        return jsonify(err[0]), err[1]
    data = request.get_json(silent=True) or {}

    token = data.get("perk_token")
    order_ref = str(data.get("order_id") or "").strip()
    if not token or not order_ref or len(order_ref) > 120 or data.get("amount") is None:
        return jsonify({"error": "perk_token, order_id and amount are required"}), 400

    body, status = _complete_external_sale(biz, token, data.get("amount"), order_ref, "external_server")
    return jsonify(body), status


# ---------------------------------------------------------------------------------------------------
# 2) Shopify "Order payment" webhook -> PerkMiner
#    Shopify signs the body with the shop's webhook secret (X-Shopify-Hmac-Sha256). The business pastes that
#    secret into the Online Purchase Guide page. The member's token rides along in the order's note attributes.
# ---------------------------------------------------------------------------------------------------
@csrf.exempt
@app.route("/api/external_sale/shopify/<int:business_id>", methods=["POST"])
def external_sale_shopify(business_id):
    biz = Business.query.get(business_id)
    secret = (biz.shopify_webhook_secret or "") if biz else ""
    if not secret:
        return jsonify({"error": "unauthorized"}), 401

    raw = request.get_data()
    got = request.headers.get("X-Shopify-Hmac-Sha256", "")
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, got):
        return jsonify({"error": "bad signature"}), 401

    data = request.get_json(silent=True) or {}
    token = None
    for attr in (data.get("note_attributes") or []):
        if isinstance(attr, dict) and attr.get("name") == "perk_token":
            token = attr.get("value")

    if not token:
        # Shopify's "Send test notification" (and orders from non-PerkMiner customers) land here.
        _log_ping(biz, "shopify-webhook")
        return jsonify({"ok": True, "note": "no perk_token on this order - nothing to record"}), 200

    if (data.get("currency") or "USD").upper() != "USD":
        app.logger.warning("shopify order %s skipped: currency %s", data.get("id"), data.get("currency"))
        return jsonify({"ok": True, "note": "non-USD order ignored"}), 200

    order_ref = "shopify-" + str(data.get("id") or "")
    body, status = _complete_external_sale(biz, token, data.get("total_price"), order_ref, "shopify_webhook")
    # permanent problems return 200 so Shopify stops retrying; a low balance returns 402 so Shopify retries later
    if status in (400, 409):
        return jsonify(body), 200
    return jsonify(body), status


# ---------------------------------------------------------------------------------------------------
# 3) Browser helper: "has my sale been recorded yet?"  (NO amount is accepted here any more)
#    Same URL as before, so Script B on sites that already installed it keeps redirecting to the receipt.
# ---------------------------------------------------------------------------------------------------
@csrf.exempt
@app.route("/api/record_external_sale", methods=["POST", "OPTIONS"])
def record_external_sale():
    if request.method == "OPTIONS":                 # browser preflight
        return _ext_json({}, 204)

    data = request.get_json(silent=True) or {}
    token = data.get("perk_token")
    if not token:
        return _ext_json({"error": "missing token"}, 400)
    try:
        payload = shop_serializer.loads(token, max_age=SHOP_TOKEN_MAX_AGE)
    except Exception:
        return _ext_json({"error": "invalid or expired token"}, 400)

    interaction = Interaction.query.get(payload.get("interaction_id"))
    if (not interaction or interaction.user_id != payload.get("user_id")
            or interaction.business_id != payload.get("business_id")):
        return _ext_json({"error": "invalid session"}, 400)

    recorded = BusinessTransaction.query.filter_by(interaction_id=interaction.id).first()
    if recorded:
        return _ext_json({"status": "ok", "receipt_url": _receipt_url(interaction.id)}, 200)
    return _ext_json({"status": "pending"}, 202)


# ---------------------------------------------------------------------------------------------------
# 4) Connection test ("beacon"): the business's server sends a SIGNED ping. It proves the site can reach
#    PerkMiner and that it holds the right key. The admin queue shows when the last ping arrived.
# ---------------------------------------------------------------------------------------------------
@csrf.exempt
@app.route("/api/ecommerce/verify-beacon", methods=["POST"])
def ecommerce_verify_beacon():
    biz, raw, err = _verify_signed_request()
    if err:
        return jsonify({"ok": False, **err[0]}), err[1]
    _log_ping(biz, "signed-ping")
    return jsonify({"ok": True, "business": biz.business_name}), 200


# ---------------------------------------------------------------------------------------------------
# 5) Guide page + key management (the business sees THEIR key only while logged in)
# ---------------------------------------------------------------------------------------------------
@app.route("/online-purchase-instructions")
def online_purchase_instructions():
    biz = Business.query.get(session["business_id"]) if session.get("business_id") else None
    sales_key = _get_or_create_sales_key(biz) if biz else None
    return render_template(
        "online_purchase_instructions.html",
        biz=biz,
        sales_key=sales_key,
        has_shopify_secret=bool(biz and biz.shopify_webhook_secret),
        shopify_webhook_url=(url_for("external_sale_shopify", business_id=biz.id, _external=True) if biz else None),
    )


@app.route("/business/ecommerce/regenerate_key", methods=["POST"])
@business_login_required
def ecommerce_regenerate_key():
    biz = Business.query.get_or_404(session["business_id"])
    biz.sales_key = secrets.token_urlsafe(32)
    db.session.commit()
    flash("New sales key created. Update it in your server code - the old key no longer works.", "success")
    return redirect(url_for("online_purchase_instructions"))


@app.route("/business/ecommerce/shopify_secret", methods=["POST"])
@business_login_required
def ecommerce_save_shopify_secret():
    biz = Business.query.get_or_404(session["business_id"])
    biz.shopify_webhook_secret = (request.form.get("shopify_secret") or "").strip()[:160] or None
    db.session.commit()
    flash("Shopify webhook secret saved." if biz.shopify_webhook_secret else "Shopify webhook secret removed.", "success")
    return redirect(url_for("online_purchase_instructions"))


# ---------------------------------------------------------------------------------------------------
# 6) Admin queue: shows, per business, whether a ping / a signed sale has arrived
# ---------------------------------------------------------------------------------------------------
@app.route("/admin/ecommerce-verifications")
@admin_required
def admin_ecommerce_verifications():
    pending_biz = Business.query.filter(
        Business.is_ecommerce_site.is_(True),
        Business.allow_website_purchases.is_(True),
        Business.online_terms_agreed.is_(True),
        Business.ecommerce_verified.is_(False)
    ).order_by(Business.business_name.asc()).all()

    verified_biz = Business.query.filter(
        Business.ecommerce_verified.is_(True)
    ).order_by(Business.business_name.asc()).all()

    ids = [b.id for b in pending_biz + verified_biz]
    ping_info, sale_info = {}, {}
    if ids:
        for bid, last, cnt in (db.session.query(EcommerceVerificationPing.business_id,
                                                func.max(EcommerceVerificationPing.created_at),
                                                func.count(EcommerceVerificationPing.id))
                               .filter(EcommerceVerificationPing.business_id.in_(ids))
                               .group_by(EcommerceVerificationPing.business_id).all()):
            ping_info[bid] = {"last": last, "count": cnt}
        for bid, last, cnt in (db.session.query(ExternalSale.business_id,
                                                func.max(ExternalSale.created_at),
                                                func.count(ExternalSale.id))
                               .filter(ExternalSale.business_id.in_(ids))
                               .group_by(ExternalSale.business_id).all()):
            sale_info[bid] = {"last": last, "count": cnt}

    return render_template(
        "admin_ecommerce_verifications.html",
        pending_biz=pending_biz,
        verified_biz=verified_biz,
        ping_info=ping_info,
        sale_info=sale_info,
        now=datetime.utcnow(),
    )
