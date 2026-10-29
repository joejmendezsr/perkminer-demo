# =====================================================================================
# Replacement for build_invite_email() in app.py
#
# Same name and first 3 arguments as before, so your existing calls keep working unchanged.
# Two OPTIONAL extras:
#     audience="both"      -> "both" (default) | "member" | "business"
#     business_join_url    -> link for the business button (defaults to join_url if you don't pass it)
#
# Needs:  import html   (at the top of app.py; add it if it isn't there)
# =====================================================================================

def build_invite_email(inviter_name, join_url, video_url, audience="both", business_join_url=None):
    esc = html.escape
    inviter = esc(inviter_name or "A friend")
    join_url = esc(join_url, quote=True)
    video_url = esc(video_url, quote=True)
    biz_url = esc(business_join_url or join_url, quote=True) if business_join_url else join_url
    audience = audience if audience in ("member", "business", "both") else "both"

    TEAL, TEAL_D, GOLD = "#2a5859", "#1e4a4b", "#ffd66b"
    FONT = "Arial, Helvetica, sans-serif"

    def button(url, label, bg=TEAL, fg="#ffffff", full=True):
        # "bulletproof" button: a table cell with bgcolor works in Outlook too
        return f"""
<table role="presentation" border="0" cellspacing="0" cellpadding="0" {'width="100%"' if full else ''} style="margin:0 auto;">
  <tr><td align="center" bgcolor="{bg}" style="border-radius:10px;">
    <a href="{url}" target="_blank" style="display:block; padding:15px 24px; font-family:{FONT}; font-size:17px; font-weight:bold; color:{fg}; text-decoration:none; border-radius:10px;">{label}</a>
  </td></tr>
</table>"""

    def bullets(items):
        lis = "".join(
            f'<tr><td valign="top" width="22" style="padding:0 0 9px; font-family:{FONT}; font-size:15px; color:{TEAL};">&#10003;</td>'
            f'<td style="padding:0 0 9px; font-family:{FONT}; font-size:15px; line-height:1.45; color:#374151;">{t}</td></tr>'
            for t in items)
        return f'<table role="presentation" border="0" cellspacing="0" cellpadding="0" width="100%">{lis}</table>'

    def card(tag, title, sub, items, btn_html, bg, border):
        return f"""
<table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:{bg}; border:1px solid {border}; border-radius:14px;">
  <tr><td style="padding:20px 18px 6px; font-family:{FONT};">
    <div style="font-size:12px; font-weight:bold; letter-spacing:1px; color:{TEAL}; text-transform:uppercase;">{tag}</div>
    <div style="font-size:21px; font-weight:bold; color:#111827; padding-top:2px;">{title}</div>
    <div style="font-size:14px; color:#4b5563; padding:4px 0 14px;">{sub}</div>
    {bullets(items)}
  </td></tr>
  <tr><td style="padding:6px 18px 20px;">{btn_html}</td></tr>
</table>"""

    member_card = card(
        "For members", "Earn while you shop", "Free to join &mdash; always free. No contracts or subscriptions.",
        [
            "<b>2% cash back</b> on up to $2,500 of any purchase from our advertisers.",
            "<b>Exclusive member perks</b> from the businesses you choose.",
            "<b>Privacy first:</b> secure messaging, no selling your info as a lead, no browsing tracking.",
            "<b>One member to one business</b> at a time &mdash; search with peace of mind.",
            "<b>Live tracking</b> of your service provider when they&rsquo;re &ldquo;on the way.&rdquo;",
        ],
        button(join_url, "Join free as a member"), "#f1f9f9", "#cfe3e3")

    business_card = card(
        "For business owners", "Advertise &mdash; pay only after you get paid", "No sale or closed deal = <b style=\"color:#dc2626;\">zero fees</b>.",
        [
            "<b>1,000% return on ad spend or higher:</b> spend $1, get $10 back.",
            "<b>10% of the sale, capped at $250</b> &mdash; only after you get paid.",
            "<b>No cost</b> for leads, calls, website or foot traffic, appointments or meetups.",
            "<b>Earn 1% cash back</b> on up to $2,500 of your sale for offering a perk to members.",
            "Just <b>$25</b> in pre-funded dollars to start (sales over $250 need more). Funds stay in your balance until you make a sale.",
            "No hidden fees, contracts or membership fees. Live provider tracking at no cost.",
        ],
        button(biz_url, "Advertise my business", bg=GOLD, fg=TEAL_D), "#fffaf0", "#f1dfa6")

    if audience == "member":
        hero = "https://res.cloudinary.com/dmrntlcfd/image/upload/v1791003112/member_flyer_gncgvf.jpg"
        headline = f"{inviter} invited you to join Perk Miner"
        pre = "Earn 2% cash back and referral commissions. Free to join, always."
        cards = f'<tr><td style="padding:0 22px 8px;">{member_card}</td></tr>'
        steps = "Register in 4 easy steps: enter a valid email and password, click the captcha, click Register, and verify your email. That&rsquo;s it!"
    elif audience == "business":
        hero = "https://res.cloudinary.com/dmrntlcfd/image/upload/v1791000401/biz-flyer_pfg6os.jpg"
        headline = f"{inviter} invited your business to advertise on Perk Miner"
        pre = "Zero fees unless you make a sale. 1,000% return on ad spend or higher."
        cards = f'<tr><td style="padding:0 22px 8px;">{business_card}</td></tr>'
        steps = "Sign up in minutes. Just $25 in pre-funded advertising dollars gets you started."
    else:
        hero = "https://res.cloudinary.com/dmrntlcfd/image/upload/v1791003112/member_flyer_gncgvf.jpg"
        headline = f"{inviter} invited you to join Perk Miner"
        pre = "Members earn cash back. Businesses pay only after they get paid."
        # two columns on wide screens, stacked on phones
        cards = f"""
<tr><td style="padding:0 14px 8px;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0">
    <tr>
      <td class="col" width="50%" valign="top" style="padding:0 8px 14px;">{member_card}</td>
      <td class="col" width="50%" valign="top" style="padding:0 8px 14px;">{business_card}</td>
    </tr>
  </table>
</td></tr>"""
        steps = "<b>Pick your side:</b> members join free in 4 easy steps (email + password, captcha, register, verify your email). Businesses start with just $25 in pre-funded ad dollars."

    everyone = f"""
<tr><td style="padding:6px 22px 8px;">
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:{TEAL}; border-radius:14px;">
    <tr><td align="center" style="padding:18px 20px; font-family:{FONT}; color:#ffffff;">
      <div style="font-size:12px; font-weight:bold; letter-spacing:1px; color:{GOLD}; text-transform:uppercase;">Everyone earns</div>
      <div style="font-size:15px; line-height:1.5; padding-top:6px;">
        Members <b>and</b> business owners earn cash back and referral commissions &mdash; paid by Perk Miner, up to <b>6 members and 6 businesses</b> on every finalized transaction.
        Up to <b>84%</b> of ad revenue goes back to the community. <b>Real money deposited to your bank account</b> &mdash; not points or gift cards.
      </div>
    </td></tr>
  </table>
</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>You have been invited to join PerkMiner.com!</title>
<style type="text/css">
  body {{ margin:0; padding:0; -webkit-font-smoothing:antialiased; -webkit-text-size-adjust:100%; }}
  table, td {{ border-collapse:collapse; }}
  img {{ border:0; outline:none; }}
  a {{ color:{TEAL}; }}
  @media only screen and (max-width:620px) {{
    .wrap {{ width:100% !important; }}
    .col {{ display:block !important; width:100% !important; padding:0 0 14px !important; box-sizing:border-box; }}
    .h1 {{ font-size:22px !important; }}
  }}
</style>
</head>
<body style="margin:0; padding:0; background-color:#eef3f3;">
<div style="display:none; max-height:0; overflow:hidden; opacity:0; color:#eef3f3;">{pre}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" bgcolor="#eef3f3">
<tr><td align="center" style="padding:20px 10px;">
  <table role="presentation" class="wrap" width="640" border="0" cellspacing="0" cellpadding="0" style="width:640px; max-width:640px; background-color:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 4px 14px rgba(0,0,0,0.08);">

    <!-- Header -->
    <tr><td align="center" bgcolor="{TEAL}" style="padding:26px 24px 22px; font-family:{FONT};">
      <div style="font-size:13px; font-weight:bold; letter-spacing:2px; color:{GOLD}; text-transform:uppercase;">You&rsquo;re invited</div>
      <div class="h1" style="font-size:27px; font-weight:bold; line-height:1.25; color:#ffffff; padding-top:6px;">{headline}</div>
    </td></tr>

    <!-- Hero image -->
    <tr><td><img src="{hero}" width="640" alt="Perk Miner" style="display:block; width:100%; height:auto;"></td></tr>

    <!-- Intro -->
    <tr><td align="center" style="padding:22px 28px 6px; font-family:{FONT}; font-size:16px; line-height:1.5; color:#374151;">
      <b>Earn cash back and referral commissions with Perk Miner.</b><br>{steps}
    </td></tr>
    <tr><td align="center" style="padding:6px 28px 18px; font-family:{FONT}; font-size:15px;">
      <a href="{video_url}" target="_blank" style="color:{TEAL}; font-weight:bold; text-decoration:underline;">&#9654; Watch our intro video</a>
    </td></tr>

    <!-- Member / Business cards -->
    {cards}

    <!-- Everyone earns -->
    {everyone}

    <!-- Footer -->
    <tr><td align="center" style="padding:22px 30px 26px; background-color:#f8f9fa; font-family:{FONT}; font-size:13px; color:#6b7280; line-height:1.5; border-top:1px solid #e5e7eb;">
      <p style="margin:0 0 6px;">Questions about this email? <a href="mailto:fromperkminer@gmail.com" style="color:{TEAL};">Contact support</a></p>
      <p style="margin:0;">Copyright &copy; PerkMiner 2026. All rights reserved.</p>
    </td></tr>

  </table>
</td></tr>
</table>
</body>
</html>"""
