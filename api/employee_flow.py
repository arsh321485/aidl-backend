"""The employee's card flow in Slack DMs — the design in E:\\slack-userflow
("AIDL Trial Flow C"): one card at a time, each a coloured attachment.

    Welcome            → "Let's go →" — then the policy cards the admin has
                         sent (policy_cards.py), each its own message with
                         "I agree" (image), agreed whenever the person likes
    Card 1 of 2 · Traffic Light Check → "Got it. Next →"   (image)
    Card 2 of 2 · The Highway Code    → "Got it. Get my license →"   (image)
    Policy cards left  → a reminder with the ones still to agree to
    Avatar             → only if they haven't picked one: create it on the
                         website (avatar.py); the Slack photo is never used
    License issued     → the licence image (licence_image.py) with the avatar
                         and the valid-till date (1 year), download, share

Every card must be approved by an admin first (card_approval.py). If the
next card isn't approved yet the employee gets a "waiting for approval"
card, and the real one follows as soon as it's approved
(post_waiting_cards).

The admin starts the flow once (Admin Center step 5); people who join the
channel later get the Welcome card automatically.
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from . import aup_store, slack_client
from .models import AIDLUser, Organization, PackageDelivery

COLOR = {"yellow": "#FFCC00", "green": "#2EC866", "blue": "#6FB3E0", "asphalt": "#14140F"}
STAGES = ("traffic_light", "highway_code", "policy", "licence")


def _t(t: str) -> dict:
    return {"type": "plain_text", "text": t, "emoji": True}


def _m(t: str) -> dict:
    return {"type": "mrkdwn", "text": t}


def _header(t):
    return {"type": "header", "text": _t(t)}


def _section(t, accessory=None):
    b = {"type": "section", "text": _m(t)}
    if accessory:
        b["accessory"] = accessory
    return b


def _context(*lines):
    return {"type": "context", "elements": [_m(x) for x in lines]}


def _button(text, action_id, value, style=None, url=None):
    b = {"type": "button", "text": _t(text), "action_id": action_id, "value": value}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    return b


def _actions(block_id, *els):
    return {"type": "actions", "block_id": block_id, "elements": list(els)}


DIVIDER = {"type": "divider"}


def _done(text: str) -> dict:
    now = int(timezone.now().timestamp())
    return _context(f":white_check_mark: {text} · <!date^{now}^{{time}}|just now>")


def card(fallback: str, label: str, color: str, blocks: list) -> dict:
    """A short label line + a coloured attachment holding the card."""
    return {"text": fallback, "blocks": [_context(label)], "attachments": [{"color": color, "blocks": blocks}]}


def _first(member: AIDLUser) -> str:
    return (member.full_name or "there").split()[0]


# ---------- cards ----------

def welcome(member: AIDLUser, org: Organization, done: bool = False) -> dict:
    from .plans import cadence

    blocks = [
        _header(f":wave: Hi {_first(member)}, welcome to your AI Driving License"),
        _context(f"{org.name} · AI Driving License programme · `{(org.plan or 'trial').upper()}`"),
        _section(f"{org.name} uses AI every day. AIDL helps you use it *safely and confidently*, one short card at a "
                 "time, right here in Slack."),
        {"type": "section", "fields": [_m("*Takes*\nAbout 10 min"), _m("*You get*\nAI Driving License"),
                                       _m(f"*After that*\n{cadence(org).capitalize()}")]},
        DIVIDER,
        _section(_route(org)),
        _done("Started onboarding") if done else _actions("aidl_flow_welcome", _button("Let's go →", "aidl_flow_start",
                                                                                         "start", "primary")),
        *([] if member.avatar_data else [_section(
            ":art: *Your licence needs an avatar.* Create it now or after the cards — it goes on your AI Driving "
            "License.", accessory=_button("🎨 Create my avatar", "aidl_link_avatar", "create",
                                          url=_avatar_url(member)))]),
        _context(":lock: Your progress is private to you and your AIDL admin."),
    ]
    return card(f"Hi {_first(member)}, welcome to your AI Driving License.", "*Welcome*", COLOR["yellow"], blocks)


def _route(org: Organization) -> str:
    n = len(aup_store.required_points(org))
    steps = [":vertical_traffic_light:  *Traffic Light Check* · what's safe to paste",
             ":book:  *The Highway Code* · 5 everyday rules"]
    if n:
        steps.insert(0, f":scroll:  *{org.name} policy* · {n} short card{'s' if n != 1 else ''} to agree to")
    return "*Your route today*\n\n" + "\n".join(steps)


def _card_image(kind: str, org: Organization) -> dict:
    """The designed card as an image (card_image.py) — with the company
    logo and watermark on Basic and Enterprise."""
    from . import card_image

    from .image_cache import predraw

    predraw(card_image.png_for, kind, org)  # drawn before Slack asks for it (Slack gives up after a few seconds)
    return {"type": "image", "image_url": card_image.image_url(kind, org), "alt_text": card_image.alt_text(kind, org)}


WAITING_LABEL = {"traffic_light": "the Traffic Light Check", "highway_code": "the Highway Code",
                 "aup": "the AI Acceptable Use Policy"}


def waiting(member: AIDLUser, org: Organization, item: str) -> dict:
    """The next card isn't approved yet: it follows as soon as it is."""
    from .admin_setup import state, update

    st = state(org)
    ids = set(st.get(f"waiting_{item}") or [])
    if str(member.pk) not in ids:
        update(org, **{f"waiting_{item}": sorted(ids | {str(member.pk)})})
    return card(f"Your next card ({WAITING_LABEL[item]}) is waiting for approval.", "*Waiting for approval*",
                COLOR["asphalt"], [
        _header(":hourglass_flowing_sand: Your next card is on its way"),
        _section(f"Your AIDL admin is approving *{WAITING_LABEL[item]}* for {org.name}. It will appear here as soon "
                 "as it's approved — nothing to do until then."),
    ])


def post_waiting_cards(org: Organization, item: str) -> int:
    """After `item` was approved: send it to everyone who was waiting."""
    from .admin_setup import state, update

    ids = state(org).get(f"waiting_{item}") or []
    if not ids:
        return 0
    update(org, **{f"waiting_{item}": []})
    sent = 0
    for member in AIDLUser.objects.filter(pk__in=ids, is_active=True):
        if stage(member, org) == item:
            post(org, member, BUILDERS[item](member, org))
            sent += 1
    return sent


def _avatar_url(member: AIDLUser) -> str:
    from .avatar import start_url

    return start_url(member)


def _approved(org: Organization, item: str) -> bool:
    from .card_approval import is_approved

    return is_approved(org, item)


def traffic(member: AIDLUser, org: Organization, done: bool = False) -> dict:
    if not done and not _approved(org, "traffic_light"):
        return waiting(member, org, "traffic_light")
    return card("Traffic Light Check. Before you paste anything into an AI, check the lights.",
                "*:vertical_traffic_light: Traffic Light Check* · Before you paste anything",
                COLOR["green"], [
        _card_image("traffic_light", org),
        _done("Traffic Light Check done") if done else _actions("aidl_flow_tl", _button(
            "Got it. Next: The Highway Code →", "aidl_flow_tl", "next", "primary")),
    ])


def highway(member: AIDLUser, org: Organization, done: bool = False) -> dict:
    if not done and not _approved(org, "highway_code"):
        return waiting(member, org, "highway_code")
    return card("The Highway Code. The everyday rules for using AI safely and confidently.",
                "*:book: Highway Code* · Section A · Learner Rules", COLOR["blue"], [
        _card_image("highway_code", org),
        _done("Highway Code done") if done else _actions("aidl_flow_hc", _button(
            "Got it. Get my license →", "aidl_flow_hc", "next", "primary")),
    ])


def policy_card(member: AIDLUser, org: Organization, n: int, agreed: bool = False) -> dict:
    """One policy card (policy_cards.py) with "I agree"."""
    from .policy_cards import ICONS, SHORT

    c = aup_store.card(org, n) or {}
    ver = c.get("version", "")
    label = f"*Policy card · {ICONS.get(n, '')} {SHORT.get(n, '')}* · {org.name} AI policy"
    if agreed:
        return card(f"{org.name} policy — {SHORT.get(n, '')}: agreed.", label, COLOR["green"], [
            _card_image(f"aup-{n}", org),
            _done("Agreed"),
        ])
    return card(f"{org.name} policy — {SHORT.get(n, '')}. Read it and press I agree.", label, COLOR["asphalt"], [
        _card_image(f"aup-{n}", org),
        _actions("aidl_flow_aup", _button("✅ I agree", "aidl_flow_aup", f"{aup_store.point_item(n)}|{ver}",
                                          "primary")),
        _context("Your agreement is time-stamped and saved to your AIDL record."),
    ])


def policy_left(member: AIDLUser, org: Organization) -> dict:
    """Traffic Light and Highway Code done; policy cards still to agree to."""
    from .policy_cards import SHORT

    left = aup_store.pending_points(member, org)
    names = "\n".join(f"›  {SHORT.get(n, n)}" for n in left)
    return card(f"Almost there — agree to {len(left)} policy card(s) for your licence.", "*Almost there*",
                COLOR["yellow"], [
        _header(":scroll: One more step for your licence"),
        _section(f"Agree to {'this policy card' if len(left) == 1 else f'these {len(left)} policy cards'} from "
                 f"{org.name} and your AI Driving License is issued:\n{names}"),
        _actions("aidl_flow_policy", _button("Show them again", "aidl_flow_policy", "show")),
    ])


def avatar_card(member: AIDLUser, org: Organization, notice: str = "") -> dict:
    """Licence earned, no avatar yet: pick one before the licence is shown."""
    from . import avatar

    return card("Your AI Driving License is ready — pick the avatar for it.", "*License · your photo*",
                COLOR["yellow"], [
        _header(":art: One last step: your licence avatar"),
        _section(f"You've earned your *AI Driving License*, {_first(member)}! Create the avatar that goes on it — "
                 "pick a ready-made one or build your own."),
        _actions("aidl_flow_avatar",
                 _button(":art: Create my avatar", "aidl_link_avatar", "create", "primary", url=avatar.start_url(member))),
        *([_context(notice)] if notice else []),
        _context("Your licence arrives here as soon as your avatar is saved."),
    ])


def licence(member: AIDLUser, org: Organization) -> dict:
    from . import avatar, licence_image

    if not member.licence_issued:
        return card("All cards done — your license is waiting for a free seat.", "*License*", COLOR["yellow"], [
            _header(":hourglass_flowing_sand: Almost there"),
            _section(f"You've finished every card. All license seats on {org.name}'s plan are in use, so your license "
                     "will be issued as soon as one is free. Your AIDL admin has been told."),
        ])
    if not member.avatar_data:
        return avatar_card(member, org)
    number = member.licence_number
    version = licence_image.url_version(member, org)
    from .image_cache import predraw

    predraw(licence_image.png_for, member, org)  # drawn before Slack asks for it
    valid = (f" · valid till *{member.licence_expires_at.strftime('%d %B %Y')}*"
             if member.licence_expires_at else "")
    issued = member.licence_issued_at or timezone.now()
    nxt = (PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED,
                                          post_at__gt=timezone.now()).order_by("post_at").first())
    when = (f"*<!date^{int(nxt.post_at.timestamp())}^{{date_long_pretty}}|soon>*" if nxt else "*soon*")
    verify = f"{settings.FRONTEND_URL.rstrip('/')}/home#verify?lic={number}"
    return card(f"You're road-ready, {_first(member)}! Here's your AI Driving License.", "*License issued*",
                COLOR["green"], [
        _header(f":tada: You're road-ready, {_first(member)}!"),
        _context("Traffic Light, Highway Code and every policy card agreed. · `ISSUED`"),
        _section(f"Here's your *AI Driving License*{valid}."),
        {"type": "image", "image_url": licence_image.image_url(number, version=version),
         "title": _t(f"AI Driving License · {number}"),
         "alt_text": f"AI Driving License for {member.full_name or member.email}, {number}, issued for {org.name} "
                     f"on {issued.strftime('%d %B %Y')}."},
        DIVIDER,
        _section(f":white_check_mark: {org.name} policy cards agreed\n:white_check_mark: Traffic Light Check\n"
                 ":white_check_mark: The Highway Code"),
        _section(f":spiral_calendar_pad: Your next card arrives {when}. Keep the Traffic Light and Highway Code "
                 "cards handy."),
        _actions("aidl_flow_licence",
                 _button(":inbox_tray: Download license", "aidl_link_download", "download",
                         url=licence_image.image_url(number, download=True, version=version)),
                 _button(":art: Change avatar", "aidl_link_avatar", "change", url=avatar.start_url(member)),
                 _button(":link: Share on LinkedIn", "aidl_link_share", "linkedin",
                         url="https://www.linkedin.com/sharing/share-offsite/?url=" + quote(verify, safe=""))),
        _actions("aidl_licence_more",
                 _button(":books: My cards", "aidl_my_cards", "0"),
                 _button(":motorway: Request an app", "aidl_app_request_open", "licence")),
    ])


# ---------- moving through the flow ----------

def _slack_id(member: AIDLUser) -> str:
    return member.microsoft_id.split(":")[-1] if member.microsoft_id.startswith("slack:") else ""


def stage(member: AIDLUser, org: Organization) -> str:
    """The card the employee is on."""
    from .licensing import acknowledged_items

    done = acknowledged_items(member)
    if "traffic_light" not in done:
        return "traffic_light"
    if "highway_code" not in done:
        return "highway_code"
    if not aup_store.all_points_accepted(member, org):
        return "policy"
    return "licence"


BUILDERS = {"traffic_light": traffic, "highway_code": highway, "policy": policy_left, "licence": licence}


def post(org: Organization, member: AIDLUser, payload: dict) -> dict:
    return slack_client.send_dm(slack_client.bot_token(org), org, _slack_id(member), text=payload["text"],
                                blocks=payload["blocks"], attachments=payload.get("attachments"))


def update(org: Organization, channel: str, ts: str, payload: dict) -> None:
    slack_client.slack_api("chat.update", slack_client.bot_token(org), json={
        "channel": channel, "ts": ts, "text": payload["text"], "blocks": payload["blocks"],
        "attachments": payload.get("attachments") or []})


def post_licence(member: AIDLUser) -> dict:
    """The avatar was saved: post the licence card (with it)."""
    from .org_service import get_organization_for_user

    org = get_organization_for_user(member)
    if org is None or not org.slack_bot_token:
        return {}
    member.refresh_from_db()
    return post(org, member, licence(member, org))


def start(org: Organization, member: AIDLUser) -> dict:
    """Send the Welcome card (once the AUP exists)."""
    return post(org, member, welcome(member, org))


def resend_current(org: Organization, member: AIDLUser) -> dict:
    """Remind: post the card the employee is on again (and the policy cards
    they haven't agreed to)."""
    now = stage(member, org)
    if now == "policy":
        _repost_policy(org, member)
    return post(org, member, BUILDERS[now](member, org))


def _repost_policy(org: Organization, member: AIDLUser) -> None:
    for n in aup_store.pending_points(member, org):
        post(org, member, policy_card(member, org, n))


def advance(org: Organization, member: AIDLUser, action_id: str, value: str, channel: str, ts: str) -> None:
    """A button on a flow card was pressed."""
    from .licensing import acknowledge, issue_learner_if_ready

    if action_id == "aidl_flow_avatar":  # an older card's "Use my Slack photo" — no longer offered
        update(org, channel, ts, avatar_card(member, org))
        return
    if action_id == "aidl_flow_start":
        from .policy_cards import deliver_to

        update(org, channel, ts, welcome(member, org, done=True))
        deliver_to(org, member)  # the policy cards (due ones now, later ones on their day)
        post(org, member, BUILDERS[stage(member, org)](member, org))
        return
    if action_id == "aidl_flow_policy":
        _repost_policy(org, member)
        return
    if action_id == "aidl_flow_aup":
        item, _, ver = value.partition("|")
        n = int(item[4:]) if aup_store.is_point_item(item) else 0
        current = aup_store.required_points(org).get(n)
        if not current:  # an older card that was replaced, or one not sent yet
            return
        if ver and ver != current:
            update(org, channel, ts, card("This card was updated — the new version is below.", "*Policy card*",
                                          COLOR["asphalt"], [_section("This card was updated. Agree to the new "
                                                                      "version in the message below.")]))
            post(org, member, policy_card(member, org, n))
            return
        acknowledge(member, item, current)
        update(org, channel, ts, policy_card(member, org, n, agreed=True))
        if stage(member, org) != "licence":
            return  # more cards to agree to, or the Traffic Light / Highway Code still ahead
    elif action_id == "aidl_flow_tl":
        acknowledge(member, "traffic_light")
        update(org, channel, ts, traffic(member, org, done=True))
    elif action_id == "aidl_flow_hc":
        acknowledge(member, "highway_code")
        update(org, channel, ts, highway(member, org, done=True))
    else:
        return
    nxt = stage(member, org)
    if nxt != "licence":
        post(org, member, BUILDERS[nxt](member, org))
        return
    if member.licence_issued:
        return  # a card sent after the licence: agreeing to it is all there is
    result = issue_learner_if_ready(member, org)
    member.refresh_from_db()
    if result == "issued":
        from .package_delivery import sync_package
        from .policy_awareness import send_to

        sync_package(org, member)  # schedules the awareness cards (the licence card shows the next date)
    post(org, member, licence(member, org))
    if result == "issued":
        send_to(org, member)  # approved awareness cards from the AI policy
    if result == "no_seats":
        _tell_admin_no_seat(org, member)


def _tell_admin_no_seat(org: Organization, member: AIDLUser) -> None:
    from .plans import user_limit
    from .slack_onboarding import primary_admin

    admin = primary_admin(org)
    if admin is None or not admin.microsoft_id.startswith("slack:"):
        return
    text = (f"⏳ *{member.full_name or member.email}* finished the policy cards, the Traffic Light Check and the Highway Code, "
            f"but all {user_limit(org)} licenses on your plan are used. Their license is waiting for a free seat.")
    slack_client.send_dm(slack_client.bot_token(org), org, admin.microsoft_id.split(":")[-1],
                         text="AIDL: no free license seats", blocks=[_section(text)])


def start_everyone(org: Organization) -> tuple[int, int]:
    """Admin step 4: send the Welcome card to every team member who hasn't
    started. Returns (sent, total)."""
    from .licensing import learners

    members = list(learners(org).filter(microsoft_id__startswith=f"slack:{org.slack_team_id}:"))
    sent = 0
    for m in members:
        if stage(m, org) == "licence" and m.licence_issued:
            continue
        r = start(org, m)
        sent += 1 if r.get("ok") or r.get("fallback_ok") else 0
    return sent, len(members)


def remind_everyone(org: Organization) -> tuple[int, int]:
    """Post each unfinished employee's current card again — and ask licensed
    people who haven't created their avatar yet (their licence card follows
    once they do)."""
    from .licensing import learners

    pending = [m for m in learners(org).filter(microsoft_id__startswith=f"slack:{org.slack_team_id}:")
               if not m.licence_issued or not m.avatar_data]
    sent = 0
    for m in pending:
        r = resend_current(org, m)
        sent += 1 if r.get("ok") or r.get("fallback_ok") else 0
    return sent, len(pending)



