"""Advertising fee plans for businesses and freelancers.

Two plans (chosen by the advertiser):

  "full" - 10% ad fee (max $250 per transaction). All features. Members get 2% cash back.
  "low"  -  2% ad fee (max  $50 per transaction). For low-margin sellers. Members get 0.4% cash back.
            No featured placement, no live tracking, no public profile page (so no search/AI
            discovery of it), and no promo codes.

Every payout on the "low" plan is exactly one fifth of the "full" plan (same $2,500 sale cap), so one
multiplier does the job: SCALE_LOW = 0.2. The one exception is the mutual commission, which is
0.2% on both plans (split among the advertisers you directly sponsor).

Freelancers use a hidden "shadow" Business row, so every helper just takes a Business.
"""
from decimal import Decimal

FULL = "full"
LOW = "low"

FEE_RATE = {FULL: 0.10, LOW: 0.02}
FEE_CAP = {FULL: 250.00, LOW: 50.00}
SCALE = {FULL: 1.0, LOW: 0.2}

LABEL = {FULL: "10% ad fee", LOW: "2% ad fee"}
CASHBACK_LABEL = {FULL: "2%", LOW: "0.4%"}

# What a 2% advertiser does not get (shown on the plan picker, the switch dialog and the terms)
LOW_MISSING = [
    "Featured placement (featured listings and the New Featured Businesses page)",
    "Live tracking of your staff and service providers",
    "A public profile page (so no search engine or AI assistant discovery of your profile)",
    "Promo codes (founding and sign-up promo codes cannot be redeemed)",
    "The 2% member cash back (your customers earn 0.4% instead)",
    "The full referral commissions for the members who send you customers (they earn one fifth)",
]

FULL_HAS = [
    "Everything: featured placement, live tracking and a public profile page",
    "Search engine and AI assistant discovery of your public profile",
    "The most cash back for your customers: 2% on every purchase, up to $50 on each one",
    "The full referral commissions for every member who refers customers to you",
    "Eligibility for promo codes",
]


def plan_of(biz):
    """'full' or 'low' for a Business (anything unexpected is treated as the full plan)."""
    if isinstance(biz, dict):          # admin previews pass a plain dict
        return LOW if biz.get("ad_fee_plan") == LOW else FULL
    return LOW if getattr(biz, "ad_fee_plan", None) == LOW else FULL


def is_low(biz):
    return plan_of(biz) == LOW


def scale(biz):
    return SCALE[plan_of(biz)]


def fee_rate(biz):
    return FEE_RATE[plan_of(biz)]


def fee_cap(biz):
    return FEE_CAP[plan_of(biz)]


def ad_fee(biz, amount):
    """Advertising fee for a sale, rounded to cents and capped for the plan."""
    return min(round(float(amount) * fee_rate(biz), 2), fee_cap(biz))


def cashback_label(biz):
    """'2%' or '0.4%': what a member earns on a purchase from this advertiser."""
    return CASHBACK_LABEL[plan_of(biz)]


def plan_label(biz):
    return LABEL[plan_of(biz)]


def dec_scale(biz):
    return Decimal(str(scale(biz)))


FINALIZATION_LABELS = {
    "Finalized-Upon-Final-Payment": "Finalized upon final payment of project",
    "Finalized-In-Steps": "Finalized in steps - up to $2,500 of entire project",
}


def finalization_label(value):
    """Display text for a business' Finalization choice (older choices are shown as they were saved)."""
    return FINALIZATION_LABELS.get(value, value)


def register(app, ns):
    """Jinja helpers so every template can show the right plan without extra view code."""
    app.jinja_env.globals.update(
        fee_plan=plan_of,
        fee_is_low=is_low,
        fee_cashback_label=cashback_label,
        fee_plan_label=plan_label,
        fee_low_missing=LOW_MISSING,
        fee_full_has=FULL_HAS,
        fin_label=finalization_label,
    )
