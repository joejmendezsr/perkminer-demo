# =====================================================================================
# COMPLETE QUOTE ROUTES  (replace your 4 existing quote routes in app.py with everything below)
#
# BEFORE you paste this, make sure these 3 things are already done (see app_py_quote_changes.py):
#   1. Ran migration_quote_line_items.sql in pgAdmin
#   2. quote_helpers.py sits next to app.py, and app.py has:   from quote_helpers import build_quote_from_form, QuoteError
#   3. class Quote has the 5 new columns and quote_items_for() sits right under it
#
# Also make sure app.py has `import json` near the top (quote_items_for needs it).
#
# Delete: user_quote_view, create_quote, view_quote, staff_create_quote  (the old versions)
# Leave alone: public_profile_url() right below them in your file.
# NOTE: every variable is named `quote_row`, never `quote`, because app.py imports urllib's quote().
# =====================================================================================


def _is_finalized(interaction):
    """True once a BusinessTransaction exists for this session (no more quotes allowed)."""
    return BusinessTransaction.query.filter_by(interaction_id=interaction.id).first() is not None


def _save_quote_from_form(interaction, quote_row, form):
    """Validates the itemized form, writes it to the Quote row and commits.
    Returns (quote_row, was_update).  Raises QuoteError for bad input (message is user-friendly)."""
    data = build_quote_from_form(form)           # raises QuoteError
    was_update = quote_row is not None
    if quote_row is None:
        quote_row = Quote(interaction_id=interaction.id)
    quote_row.amount = float(data["total"])      # total the member pays
    quote_row.details = data["details"]          # readable text version (older screens still use it)
    quote_row.line_items = json.dumps(data["items"])
    quote_row.subtotal = data["subtotal"]
    quote_row.tax_rate = data["tax_rate"]
    quote_row.tax_amount = data["tax_amount"]
    quote_row.notes = data["notes"]
    db.session.add(quote_row)
    db.session.commit()
    return quote_row, was_update


# USER — View Quote (read-only; user context)
@app.route("/session/<int:interaction_id>/user-quote")
@login_required
def user_quote_view(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        abort(403)
    quote_row = Quote.query.filter_by(interaction_id=interaction.id).first()
    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote_row,
        quote_items=quote_items_for(quote_row),
        is_user=True,
        is_biz=False,
        is_staff=False,
        is_finalized=_is_finalized(interaction),
        error_message=None,
    )


# BUSINESS — Create / Edit Quote (business context)
@app.route("/session/<int:interaction_id>/quote", methods=["GET", "POST"])
@business_login_required
def create_quote(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    is_biz = session.get("business_id") == interaction.business_id
    if not is_biz:
        flash("Only the business can send or edit a quote for this session.")
        return redirect(url_for("biz_active_session", interaction_id=interaction_id))

    is_finalized = _is_finalized(interaction)
    quote_row = Quote.query.filter_by(interaction_id=interaction.id).first()
    error_message = None

    if request.method == "POST":
        if is_finalized:
            flash("This session has been finalized. No further quotes may be sent.", "warning")
            return redirect(url_for("biz_active_session", interaction_id=interaction.id))
        try:
            quote_row, was_update = _save_quote_from_form(interaction, quote_row, request.form)
            flash("Quote was updated!" if was_update else "Quote sent to user!", "success")
            return redirect(url_for("biz_active_session", interaction_id=interaction.id))
        except QuoteError as e:
            db.session.rollback()
            error_message = str(e)
        except Exception as e:
            db.session.rollback()
            app.logger.exception("create_quote failed")
            error_message = "Failed to save quote. Please try again."
        # re-read so the page shows what is really saved, not half-applied edits
        quote_row = Quote.query.filter_by(interaction_id=interaction.id).first()

    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote_row,
        quote_items=quote_items_for(quote_row),
        is_user=False,
        is_biz=True,
        is_staff=False,
        is_finalized=is_finalized,
        error_message=error_message,
    )


# MEMBER shortcut URL. The old version had @business_login_required but checked for the member (so it could
# never work for a member). It now just sends the member to the real read-only quote page.
@app.route("/session/<int:interaction_id>/quote/view")
@login_required
def view_quote(interaction_id):
    interaction = Interaction.query.get_or_404(interaction_id)
    if interaction.user_id != current_user.id:
        flash("Only the user may view the quote.")
        return redirect(url_for("active_session", interaction_id=interaction_id))
    return redirect(url_for("user_quote_view", interaction_id=interaction_id))


# STAFF — Create / Edit Quote
@app.route("/staff/session/<int:interaction_id>/quote", methods=["GET", "POST"])
def staff_create_quote(interaction_id):
    staff_id = session.get("staff_id")
    if not staff_id:
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    staff = Staff.query.get(staff_id)
    if not staff:
        session.pop("staff_id", None)
        flash("Please log in as staff.")
        return redirect(url_for("staff_login"))
    interaction = Interaction.query.filter_by(id=interaction_id, business_id=staff.business_id).first_or_404()

    # Finalized sessions cannot be quoted
    is_finalized = _is_finalized(interaction)
    if is_finalized:
        flash("This session has been finalized. No further quotes may be sent.", "warning")
        return redirect(url_for("staff_active_session", interaction_id=interaction.id))

    quote_row = Quote.query.filter_by(interaction_id=interaction.id).first()
    error_message = None

    if request.method == "POST":
        try:
            quote_row, was_update = _save_quote_from_form(interaction, quote_row, request.form)
            flash("Quote was updated!" if was_update else "Quote sent to user!", "success")
            return redirect(url_for("staff_active_session", interaction_id=interaction.id))
        except QuoteError as e:
            db.session.rollback()
            error_message = str(e)
        except Exception as e:
            db.session.rollback()  # always roll back on error
            app.logger.exception("staff_create_quote failed")
            error_message = "Failed to save quote. Please try again."
        quote_row = Quote.query.filter_by(interaction_id=interaction.id).first()

    return render_template(
        "quote.html",
        interaction=interaction,
        quote=quote_row,
        quote_items=quote_items_for(quote_row),
        is_user=False,
        is_biz=False,
        is_staff=True,
        is_finalized=is_finalized,
        error_message=error_message,
    )
