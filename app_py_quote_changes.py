# =====================================================================================
# app.py changes for itemized quotes.  Three small additions + one change in your quote routes.
# =====================================================================================

# ---- 1) IMPORT: add this line right under  `from docuseries_routes import docuseries_bp`  (top of app.py)
# quote_helpers.py is a new file that lives in the SAME folder as app.py and docuseries_routes.py.
from quote_helpers import build_quote_from_form, QuoteError


# ---- 2) MODEL: replace your current `class Quote(db.Model)` with this (the first 5 columns are unchanged)
class Quote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    interaction_id = db.Column(db.Integer, db.ForeignKey('interaction.id'), nullable=False, unique=True)
    amount = db.Column(db.Float, nullable=False)          # the TOTAL (what the member pays)
    details = db.Column(db.Text, nullable=False)          # readable itemized text (older screens still use it)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # NEW: itemized quote
    line_items = db.Column(db.Text)                       # JSON list: [{"desc","qty","price","total"}, ...]
    subtotal = db.Column(db.Numeric(12, 2))
    tax_rate = db.Column(db.Numeric(6, 3))                # percent, e.g. 8.250
    tax_amount = db.Column(db.Numeric(12, 2))
    notes = db.Column(db.Text)

    interaction = db.relationship('Interaction', backref=db.backref('quote', uselist=False))


# ---- 3) HELPER: put this directly under the Quote class
def quote_items_for(q):
    """Line items of a quote as a list for quote.html (empty for older quotes)."""
    try:
        return json.loads(q.line_items) if q is not None and q.line_items else []
    except ValueError:
        return []


# ---- 4) YOUR QUOTE ROUTES (I have not seen them; adapt the names to what you already have).
# Keep ALL of your existing login/ownership/is_finalized checks and the render_template flags you already pass
# (is_user / is_biz / is_staff / is_finalized). Only the lines marked NEW change.
#
#   in create_quote (business/staff), POST branch:
#
#       try:
#           q = build_quote_from_form(request.form)                           # NEW (replaces reading amount/details)
#       except QuoteError as e:                                               # NEW
#           return render_template("quote.html", interaction=interaction,     # NEW  (same flags as your GET render)
#                                  quote=interaction.quote,
#                                  quote_items=quote_items_for(interaction.quote),
#                                  error_message=str(e), is_user=False, is_biz=is_biz, is_staff=is_staff,
#                                  is_finalized=False)
#
#       quote_row = interaction.quote or Quote(interaction_id=interaction.id)   # NOTE: not named "quote" (urllib's quote is imported in app.py)
#       quote_row.amount = float(q["total"])
#       quote_row.details = q["details"]
#       quote_row.line_items = json.dumps(q["items"])
#       quote_row.subtotal = q["subtotal"]
#       quote_row.tax_rate = q["tax_rate"]
#       quote_row.tax_amount = q["tax_amount"]
#       quote_row.notes = q["notes"]
#       db.session.add(quote_row)
#       db.session.commit()
#       ... then whatever you already do next (flash, redirect back to the session)
#
#   in EVERY render_template("quote.html", ...) call (create_quote GET, user_quote_view), add:
#
#       quote_items=quote_items_for(interaction.quote),
