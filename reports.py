"""Finance reports: Combined Detailed Report and Grand Total Earnings & Sales Report.

Both reports read the stored UserTransaction / BusinessTransaction rows and split them into
Members, Businesses and Freelancers. A freelancer is a Business row with owner_user_id set (the
hidden "shadow" business), so no extra columns are needed.

Both reports can be searched, printed, downloaded as PDF and exported to CSV or Excel (.xlsx).
Registered from app.py: reports.register(app, globals()).
"""
import csv
import io
from datetime import datetime

MAX_HTML_ROWS = 2000

# (referral field, amount field, label)
USER_PURCHASE = [
    ("user_referral_id", "cash_back", "Tier 1 Cashback"),
    ("tier2_user_referral_id", "tier2_commission", "Tier 2 Commission"),
    ("tier3_user_referral_id", "tier3_commission", "Tier 3 Commission"),
    ("tier4_user_referral_id", "tier4_commission", "Tier 4 Commission"),
    ("tier5_user_referral_id", "tier5_commission", "Tier 5 Commission"),
]
USER_SALE = [
    ("tier1_business_user_referral_id", "tier1_business_user_commission", "Tier 1 Commission"),
    ("tier2_business_user_referral_id", "tier2_business_user_commission", "Tier 2 Commission"),
    ("tier3_business_user_referral_id", "tier3_business_user_commission", "Tier 3 Commission"),
    ("tier4_business_user_referral_id", "tier4_business_user_commission", "Tier 4 Commission"),
    ("tier5_business_user_referral_id", "tier5_business_user_commission", "Tier 5 Commission"),
]
BIZ_FIELDS = [
    ("business_referral_id", "cash_back", "Tier 1 Cashback (own sale)"),
    ("tier2_business_referral_id", "tier2_commission", "Tier 2 Commission"),
    ("tier3_business_referral_id", "tier3_commission", "Tier 3 Commission"),
    ("tier4_business_referral_id", "tier4_commission", "Tier 4 Commission"),
    ("tier5_business_referral_id", "tier5_commission", "Tier 5 Commission"),
    ("sponsoree_mutual_referral_id", "sponsoree_mutual_commission", "Mutual Commission"),
]

DETAIL_HEADERS = ["Date", "Interaction ID", "Referral ID", "Name", "Earning Type", "Purchase / Sale",
                  "Purchase From / Sale By (Ref ID)", "Purchase From / Sale By (Name)", "Transaction Amount", "Earned"]
MEMBER_DETAIL_HEADERS = DETAIL_HEADERS + ["Member Grand Total"]


def register(app, ns):
    db = ns["db"]
    from flask import Response, abort, redirect, render_template, request, url_for
    from functools import wraps
    from flask_login import current_user, login_required

    def role_required(_name):
        """Finance staff and super admins."""
        def deco(f):
            @wraps(f)
            @login_required
            def wrapper(*a, **kw):
                if not (current_user.has_role("finance") or current_user.has_role("super_admin")):
                    abort(403)
                return f(*a, **kw)
            return wrapper
        return deco
    User = ns["User"]
    Business = ns["Business"]
    UserTransaction = ns["UserTransaction"]
    BusinessTransaction = ns["BusinessTransaction"]

    # ------------------------------------------------------------------ helpers
    def money(x):
        return f"{float(x or 0):,.2f}"

    def to_int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return 0

    def filters_from(args):
        keys = ("day", "month", "year", "interaction_id", "ref", "name", "seller_ref", "seller_name", "amount", "q")
        return {k: (args.get(k) or "").strip() for k in keys}

    def apply_date(query, col, f):
        y, m, d = to_int(f.get("year")), to_int(f.get("month")), to_int(f.get("day"))
        if y:
            query = query.filter(db.extract("year", col) == y)
        if m:
            query = query.filter(db.extract("month", col) == m)
        if d:
            query = query.filter(db.extract("day", col) == d)
        return query

    def amount_ok(amount, spec):
        """'100' = exactly 100; '100-500' = range; '>=100' / '<=500' also work."""
        spec = (spec or "").replace("$", "").replace(",", "").strip()
        if not spec:
            return True
        a = float(amount or 0)
        try:
            if spec.startswith(">="):
                return a >= float(spec[2:])
            if spec.startswith("<="):
                return a <= float(spec[2:])
            if "-" in spec[1:]:
                lo, hi = spec.split("-", 1)
                return float(lo) <= a <= float(hi)
            return abs(a - float(spec)) < 0.005
        except ValueError:
            return True

    def contains(hay, needle):
        return (not needle) or needle.lower() in (hay or "").lower()

    def lookups():
        users = {u.referral_code: u for u in User.query.all() if u.referral_code}
        biz = {b.referral_code: b for b in Business.query.all() if b.referral_code}
        return users, biz

    def is_fl(b):
        return bool(b and b.owner_user_id)

    def when(t):
        return t.date_time.strftime("%Y-%m-%d %I:%M %p") if t.date_time else ""

    # ------------------------------------------------------------------ detailed report rows
    def detailed_rows(tab, f):
        users, biz = lookups()
        out = []
        iid = f.get("interaction_id")
        if tab == "members":
            q = apply_date(UserTransaction.query, UserTransaction.date_time, f)
            if iid:
                q = q.filter(UserTransaction.interaction_id == to_int(iid))
            for t in q.all():
                seller = biz.get(t.business_referral_id)
                seller_kind = "Freelancer" if is_fl(seller) else "Business"
                if not amount_ok(t.amount, f.get("amount")):
                    continue
                if not contains(t.business_referral_id, f.get("seller_ref")) or \
                        not contains(seller.business_name if seller else "", f.get("seller_name")):
                    continue
                for group, kind in ((USER_PURCHASE, "Purchase"), (USER_SALE, "Sale")):
                    for ref_f, amt_f, label in group:
                        ref, amt = getattr(t, ref_f), getattr(t, amt_f) or 0
                        u = users.get(ref) if ref else None
                        if not u or amt <= 0:
                            continue
                        if not contains(ref, f.get("ref")) or not contains(u.name, f.get("name")):
                            continue
                        etype = (f"{label} - Purchase from {seller_kind}" if kind == "Purchase"
                                 else f"{label} - Referred {seller_kind} Sale")
                        out.append({"date": when(t), "interaction_id": t.interaction_id, "ref": ref, "name": u.name or "",
                                    "etype": etype, "kind": kind, "seller_ref": t.business_referral_id or "",
                                    "seller_name": seller.business_name if seller else "", "seller_type": seller_kind,
                                    "amount": float(t.amount or 0), "earned": float(amt), "_tier": label})
            totals = {}
            for r in out:
                totals[r["ref"]] = totals.get(r["ref"], 0) + r["earned"]
            for r in out:
                r["member_total"] = round(totals[r["ref"]], 2)
        else:
            want_fl = (tab == "freelancers")
            q = apply_date(BusinessTransaction.query, BusinessTransaction.date_time, f)
            if iid:
                q = q.filter(BusinessTransaction.interaction_id == to_int(iid))
            for t in q.all():
                seller = biz.get(t.business_referral_id)
                if not amount_ok(t.amount, f.get("amount")):
                    continue
                if not contains(t.business_referral_id, f.get("seller_ref")) or \
                        not contains(seller.business_name if seller else "", f.get("seller_name")):
                    continue
                for ref_f, amt_f, label in BIZ_FIELDS:
                    ref, amt = getattr(t, ref_f), getattr(t, amt_f) or 0
                    b = biz.get(ref) if ref else None
                    if not b or amt <= 0 or is_fl(b) != want_fl:
                        continue
                    if not contains(ref, f.get("ref")) or not contains(b.business_name, f.get("name")):
                        continue
                    out.append({"date": when(t), "interaction_id": t.interaction_id, "ref": ref,
                                "name": b.business_name or "", "etype": label, "kind": "Sale",
                                "seller_ref": t.business_referral_id or "",
                                "seller_name": seller.business_name if seller else "",
                                "seller_type": "Freelancer" if is_fl(seller) else "Business",
                                "amount": float(t.amount or 0), "earned": float(amt), "_tier": label})
        order = {"Tier 1": 1, "Tier 2": 2, "Tier 3": 3, "Tier 4": 4, "Tier 5": 5, "Mutua": 6}
        out.sort(key=lambda r: (r["interaction_id"] or 0, r["ref"], order.get(r["_tier"][:6], 9), r["kind"]))
        return out

    def detail_table(tab, rows):
        headers = MEMBER_DETAIL_HEADERS if tab == "members" else DETAIL_HEADERS
        if tab != "members":
            headers = [h.replace("Purchase / Sale", "Type").replace("Purchase From / Sale By", "Sale By") for h in headers]
        data = []
        for r in rows:
            line = [r["date"], r["interaction_id"], r["ref"], r["name"], r["etype"], r["kind"],
                    r["seller_ref"], r["seller_name"], r["amount"], r["earned"]]
            if tab == "members":
                line.append(r["member_total"])
            data.append(line)
        return headers, data

    # ------------------------------------------------------------------ grand total report
    def grand_rows(tab, f):
        users, biz = lookups()
        q = f.get("q", "")
        rows = {}
        if tab == "members":
            uq = apply_date(UserTransaction.query, UserTransaction.date_time, f)
            for t in uq.all():
                seller = biz.get(t.business_referral_id)
                fl = is_fl(seller)
                for ref_f, amt_f, _ in USER_PURCHASE:
                    ref, amt = getattr(t, ref_f), getattr(t, amt_f) or 0
                    if ref in users and amt > 0:
                        r = rows.setdefault(ref, {"bp": 0.0, "fp": 0.0, "bs": 0.0, "fs": 0.0})
                        r["fp" if fl else "bp"] += amt
                for ref_f, amt_f, _ in USER_SALE:
                    ref, amt = getattr(t, ref_f), getattr(t, amt_f) or 0
                    if ref in users and amt > 0:
                        r = rows.setdefault(ref, {"bp": 0.0, "fp": 0.0, "bs": 0.0, "fs": 0.0})
                        r["fs" if fl else "bs"] += amt
            out = []
            for ref, r in rows.items():
                u = users[ref]
                if not (contains(ref, q) or contains(u.name, q)):
                    continue
                out.append([ref, u.name or "", round(r["bp"], 2), round(r["fp"], 2), round(r["bs"], 2), round(r["fs"], 2),
                            round(r["bp"] + r["fp"] + r["bs"] + r["fs"], 2)])
            headers = ["Referral ID", "Name", "Business Purchase Earnings", "Freelancer Purchase Earnings",
                       "M2B Sales Earnings", "M2F Sales Earnings", "Grand Total"]
            out.sort(key=lambda x: (-x[-1], x[1].lower()))
            return headers, out
        want_fl = (tab == "freelancers")
        bq = apply_date(BusinessTransaction.query, BusinessTransaction.date_time, f)
        for t in bq.all():
            main = not t.sponsoree_mutual_referral_id
            for ref_f, amt_f, label in BIZ_FIELDS:
                ref, amt = getattr(t, ref_f), getattr(t, amt_f) or 0
                b = biz.get(ref) if ref else None
                if not b or amt <= 0 or is_fl(b) != want_fl:
                    continue
                r = rows.setdefault(ref, {"cb": 0.0, "com": 0.0, "mut": 0.0, "n": 0, "gross": 0.0})
                if ref_f == "business_referral_id":
                    r["cb"] += amt
                    r["n"] += 1 if main else 0
                    r["gross"] += float(t.amount or 0) if main else 0
                elif ref_f == "sponsoree_mutual_referral_id":
                    r["mut"] += amt
                else:
                    r["com"] += amt
        out = []
        for ref, r in rows.items():
            b = biz[ref]
            if not (contains(ref, q) or contains(b.business_name, q)):
                continue
            out.append([ref, b.business_name or "", r["n"], round(r["gross"], 2), round(r["cb"], 2), round(r["com"], 2),
                        round(r["mut"], 2), round(r["cb"] + r["com"] + r["mut"], 2)])
        headers = ["Referral ID", "Name", "Finalized Sales", "Gross Sales", "Sales Cash Back",
                   "Tier 2-5 Commissions", "Mutual Commissions", "Grand Total"]
        out.sort(key=lambda x: (-x[-1], x[1].lower()))
        return headers, out

    # ------------------------------------------------------------------ exporters
    def is_money(header):
        h = header.lower()
        return "type" not in h and any(w in h for w in ("amount", "earn", "total", "cash back", "commission", "gross"))

    def respond(title, headers, data, fmt, filename):
        stamp = datetime.utcnow().strftime("%Y%m%d")
        if fmt == "csv":
            si = io.StringIO()
            w = csv.writer(si)
            w.writerow(headers)
            for row in data:
                w.writerow([f"{c:.2f}" if isinstance(c, float) else c for c in row])
            return Response("﻿" + si.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment;filename={filename}_{stamp}.csv"})
        if fmt == "xlsx":
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Alignment, Font, PatternFill
                from openpyxl.utils import get_column_letter
            except ImportError:
                return respond(title, headers, data, "csv", filename)
            wb = Workbook()
            ws = wb.active
            ws.title = title[:31]
            ws.append(headers)
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1E4A4B")
                c.alignment = Alignment(wrap_text=True, vertical="center")
            for row in data:
                ws.append(row)
            for ci, h in enumerate(headers, start=1):
                width = max([len(str(h))] + [len(str(r[ci - 1])) for r in data[:300]]) + 2
                ws.column_dimensions[get_column_letter(ci)].width = min(max(width, 10), 42)
                if is_money(h):
                    for cell in ws[get_column_letter(ci)][1:]:
                        cell.number_format = '#,##0.00'
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            buf = io.BytesIO()
            wb.save(buf)
            return Response(buf.getvalue(),
                            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment;filename={filename}_{stamp}.xlsx"})
        if fmt == "pdf":
            try:
                from reportlab.lib import colors
                from reportlab.lib.pagesizes import landscape, letter
                from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
                from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
            except ImportError:
                return respond(title, headers, data, "csv", filename)
            buf = io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=24, rightMargin=24,
                                    topMargin=28, bottomMargin=28, title=title)
            st = getSampleStyleSheet()
            cell = ParagraphStyle("c", parent=st["Normal"], fontSize=6.5, leading=8)
            head = ParagraphStyle("h", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
            table = [[Paragraph(str(h), head) for h in headers]]
            for row in data:
                table.append([Paragraph(("$" + money(c)) if isinstance(c, float) else str(c if c is not None else ""), cell)
                              for c in row])
            width = landscape(letter)[0] - 48
            weights = [1.4 if is_money(h) else (2.2 if h in ("Name", "Earning Type") or "Name" in h else 1.2) for h in headers]
            cw = [width * w / sum(weights) for w in weights]
            t = Table(table, colWidths=cw, repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e4a4b")),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f7f7")]),
                                   ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d6d6")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            doc.build([Paragraph(f"Perk Miner LLC - {title}", st["Title"]),
                       Paragraph("Generated " + datetime.utcnow().strftime("%b %d, %Y %I:%M %p UTC"), st["Normal"]),
                       Spacer(1, 8), t])
            return Response(buf.getvalue(), mimetype="application/pdf",
                            headers={"Content-Disposition": f"attachment;filename={filename}_{stamp}.pdf"})
        abort(404)

    def fmt_data(data):
        """Template display copy: floats -> money strings."""
        return [[money(c) if isinstance(c, float) else c for c in row] for row in data]

    TABS = [("members", "Members"), ("businesses", "Businesses"), ("freelancers", "Freelancers")]

    def tab_of(args):
        t = args.get("tab", "members")
        return t if t in dict(TABS) else "members"

    # ------------------------------------------------------------------ routes
    @app.route("/finance/combined-detailed-report", methods=["GET"], endpoint="combined_detailed_report")
    @role_required("finance")
    def combined_detailed_report():
        tab, f = tab_of(request.args), filters_from(request.args)
        rows = detailed_rows(tab, f)
        headers, data = detail_table(tab, rows)
        total = sum(r["earned"] for r in rows)
        shown = fmt_data(data[:MAX_HTML_ROWS])
        return render_template("finance_report_detailed.html", tab=tab, tabs=TABS, f=f, headers=headers, data=shown,
                               count=len(rows), total=money(total), truncated=len(rows) > MAX_HTML_ROWS,
                               max_rows=MAX_HTML_ROWS, params={k: v for k, v in f.items() if v},
                               years=list(range(2026, 2041)))

    @app.route("/finance/combined-detailed-report/export/<fmt>", endpoint="export_combined_detailed_report")
    @role_required("finance")
    def export_combined_detailed_report(fmt):
        tab, f = tab_of(request.args), filters_from(request.args)
        headers, data = detail_table(tab, detailed_rows(tab, f))
        name = dict(TABS)[tab]
        return respond(f"Combined Detailed Report - {name}", headers, data, fmt, f"perkminer_detailed_{tab}")

    @app.route("/finance/combined-detailed-report/export/csv", endpoint="export_combined_detailed_report_csv")
    @role_required("finance")
    def export_combined_detailed_report_csv():
        return export_combined_detailed_report("csv")

    @app.route("/finance/grand-total-report", methods=["GET"], endpoint="grand_total_report")
    @role_required("finance")
    def grand_total_report():
        tab, f = tab_of(request.args), filters_from(request.args)
        headers, data = grand_rows(tab, f)
        grand = sum(r[-1] for r in data)
        return render_template("finance_report_grand.html", tab=tab, tabs=TABS, f=f, headers=headers,
                               data=fmt_data(data[:MAX_HTML_ROWS]), count=len(data), total=money(grand),
                               truncated=len(data) > MAX_HTML_ROWS, max_rows=MAX_HTML_ROWS,
                               params={k: v for k, v in f.items() if v}, years=list(range(2026, 2041)))

    @app.route("/finance/grand-total-report/export/<fmt>", endpoint="export_grand_total_report")
    @role_required("finance")
    def export_grand_total_report(fmt):
        tab, f = tab_of(request.args), filters_from(request.args)
        headers, data = grand_rows(tab, f)
        return respond(f"Grand Total Earnings and Sales - {dict(TABS)[tab]}", headers, data, fmt,
                       f"perkminer_grand_total_{tab}")

    ns["reports_detailed_rows"] = detailed_rows
    ns["reports_grand_rows"] = grand_rows
