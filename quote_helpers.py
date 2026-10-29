"""
Server-side handling of the itemized quote form (quote.html).

The page sends: item_desc[], item_qty[], item_price[], tax_rate, notes  (plus amount + details for older routes).
Totals are ALWAYS recalculated here, never trusted from the browser.

Usage in your create_quote route (POST):

    from quote_helpers import build_quote_from_form, QuoteError
    try:
        q = build_quote_from_form(request.form)
    except QuoteError as e:
        return render_template("quote.html", ..., error_message=str(e))

    quote.amount  = q["total"]        # what you already store
    quote.details = q["details"]      # readable itemized text, so older views keep working
    # optional, if you add the columns (see migration_quote_line_items.sql):
    quote.line_items = json.dumps(q["items"]); quote.subtotal = q["subtotal"]
    quote.tax_rate = q["tax_rate"];  quote.tax_amount = q["tax_amount"]; quote.notes = q["notes"]

When showing a quote, pass the items to the template so it can draw the table:

    quote_items = json.loads(quote.line_items) if getattr(quote, "line_items", None) else []
    render_template("quote.html", ..., quote_items=quote_items)
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MAX_ITEMS = 50
MAX_AMOUNT = Decimal("1000000")
CENT = Decimal("0.01")


class QuoteError(ValueError):
    pass


def _dec(value, field):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise QuoteError(f"{field} must be a number.")
    if not d.is_finite():
        raise QuoteError(f"{field} must be a number.")
    return d


def _money(d):
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _fmt(d):
    return f"${d:,.2f}"


def _qty_text(q):
    return format(q.normalize(), "f")


def build_quote_from_form(form):
    descs = form.getlist("item_desc")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    n = max(len(descs), len(qtys), len(prices))
    if n > MAX_ITEMS:
        raise QuoteError(f"A quote can have at most {MAX_ITEMS} line items.")

    items = []
    for i in range(n):
        desc = (descs[i] if i < len(descs) else "").strip()
        qty_raw = (qtys[i] if i < len(qtys) else "1").strip() or "1"
        price_raw = (prices[i] if i < len(prices) else "").strip()
        if not desc and not price_raw:
            continue  # blank row
        if not desc:
            raise QuoteError("Each line item needs a description.")
        if not price_raw:
            raise QuoteError(f'Enter a price for "{desc[:60]}".')
        qty = _dec(qty_raw, "Quantity")
        price = _dec(price_raw, "Price")
        if qty <= 0:
            raise QuoteError(f'Quantity must be more than 0 for "{desc[:60]}".')
        if price < 0:
            raise QuoteError("Prices cannot be negative.")
        price = _money(price)          # prices are whole cents, so price x qty always matches what is shown
        total = _money(qty * price)
        if total > MAX_AMOUNT:
            raise QuoteError("A line item is too large.")
        items.append({"desc": desc[:200], "qty": qty, "price": _money(price), "total": total})

    if not items:
        raise QuoteError("Add at least one line item with a description and a price.")

    rate_raw = (form.get("tax_rate") or "0").strip() or "0"
    rate = _dec(rate_raw, "Tax rate")
    if rate < 0 or rate > 100:
        raise QuoteError("Tax rate must be between 0 and 100.")

    subtotal = _money(sum((it["total"] for it in items), Decimal("0")))
    tax = _money(subtotal * rate / Decimal(100))
    total = subtotal + tax
    if total <= 0:
        raise QuoteError("The total must be more than $0.00.")
    if total > MAX_AMOUNT:
        raise QuoteError("The total is too large.")

    notes = (form.get("notes") or "").strip()[:2000]

    lines = ["Itemized quote:"]
    for it in items:
        lines.append(f"- {it['desc']}: {it['qty'].normalize():f} x {_fmt(it['price'])} = {_fmt(it['total'])}")
    lines.append(f"Subtotal: {_fmt(subtotal)}")
    if tax > 0:
        lines.append(f"Tax ({rate.normalize():f}%): {_fmt(tax)}")
    lines.append(f"Total: {_fmt(total)}")
    if notes:
        lines += ["", f"Notes: {notes}"]

    return {
        # JSON-friendly items (floats) for storing and for the template
        "items": [{"desc": it["desc"], "qty": float(it["qty"]), "price": float(it["price"]), "total": float(it["total"])} for it in items],
        "subtotal": subtotal, "tax_rate": rate, "tax_amount": tax, "total": total,
        "notes": notes, "details": "\n".join(lines),
    }
