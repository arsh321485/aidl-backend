"""The admin's "Choose from your company directory" in the Gmail side panel: everyone in the
company's Google directory with a tick box (all ticked to start), "Select all" / "Unselect all",
and "Invite selected" (the same invite email as typing addresses, ops.invite). "Invite everyone"
(on the Admin Center and here) invites the whole directory in one click.

People already in AIDL are listed as joined, without a box. The list comes from Google's
directory (google_api.directory) with the admin's own access; when the company's Google
settings don't allow it, the card says so and the email box still works.
"""

from __future__ import annotations

from api.models import AIDLUser, Organization

from . import addon as ui
from . import google_api, ops
from .models import WorkspaceInvite

DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
NO_SCOPE = ("AIDL doesn't have permission to read your company directory yet. Open the AIDL panel again and "
            "allow it when Google asks. Until then, type your colleagues' emails in Invite your team.")
MAX_ROWS = 300  # tick boxes in one card; more people: invite the rest by email or a group address


def people(event: dict, user: AIDLUser, org: Organization) -> tuple[list[dict], str]:
    """([{email, name, joined}], problem). Everyone in the domain except the admin themself.
    Read once per request (kept on the event)."""
    if "_aidl_people" not in event:
        event["_aidl_people"] = _people(event, user, org)
    return event["_aidl_people"]


def missing_scope(event: dict, user: AIDLUser, org: Organization) -> bool:
    """True when the admin hasn't allowed AIDL to read the directory (Google's granular consent)."""
    return people(event, user, org)[1] == NO_SCOPE


def _people(event: dict, user: AIDLUser, org: Organization) -> tuple[list[dict], str]:
    domain = user.email.rsplit("@", 1)[-1].lower() if "@" in user.email else ""
    try:
        found = google_api.directory(event, domain)
    except google_api.GoogleApiError as exc:
        if str(exc) == "scope":
            return [], NO_SCOPE
        if str(exc) == "refused":
            return [], ("Your company's Google settings hide the directory. A Google Workspace admin can turn it "
                        "on (admin.google.com > Directory > Directory settings > Sharing settings > Contact sharing "
                        "> Enable contact sharing), or open AIDL with a Google Workspace admin account. Until then, "
                        "type your colleagues' emails in Invite your team.")
        return [], str(exc)
    joined = {e.lower() for e in AIDLUser.objects.filter(organization_id=str(org.pk))
              .values_list("email", flat=True) if e}
    return [{**p, "joined": p["email"] in joined} for p in found if p["email"] != user.email.lower()], ""


def invite_everyone(event: dict, user: AIDLUser, org: Organization) -> str:
    """One click: invite everyone in the directory who isn't in AIDL and hasn't been invited yet."""
    found, problem = people(event, user, org)
    if problem:
        return problem
    invited = {e.lower() for e in WorkspaceInvite.objects.filter(organization_id=str(org.pk))
               .values_list("email", flat=True)}
    todo = [p["email"] for p in found if not p["joined"] and p["email"] not in invited]
    if not todo:
        return f"Everyone at {org.name} is already in AIDL or has an invite. 🎉"
    notice = ops.invite(user, org, ",".join(todo))
    if len(todo) > ops.MAX_INVITES:
        notice += f" {len(todo) - ops.MAX_INVITES} more to go: click “Invite everyone” again."
    return notice


def card(event: dict, user: AIDLUser, org: Organization, select: str = "all", notice: str = "") -> dict:
    found, problem = people(event, user, org)
    if problem:
        return ui._card("Your company directory", org.name, [
            ui._section([ui._text(ui._esc(problem)), ui._buttons(ui._button("← Back", "home"))])], notice)
    open_ = [p for p in found if not p["joined"]]
    joined = [p for p in found if p["joined"]]
    shown = open_[:MAX_ROWS]
    widgets = [ui._text(f"<b>{len(found)}</b> people at {ui._esc(org.name)} · <b>{len(joined)}</b> already in AIDL · "
                        f"<b>{len(open_)}</b> to invite")]
    if shown:
        widgets.append(ui._buttons(ui._button(f"✉️ Invite everyone ({len(open_)})", "dir_invite_all", filled=True)))
        widgets.append(ui._text("Or tick the people you want:"))
        widgets.append({"selectionInput": {
            "name": "people", "label": "Who gets an invite", "type": "CHECK_BOX",
            "items": [{"text": f"{p['name']} · {p['email']}" if p["name"] else p["email"], "value": p["email"],
                       "selected": select != "none"} for p in shown]}})
        widgets.append(ui._buttons(ui._button("Select all", "dir_open", select="all"),
                                   ui._button("Unselect all", "dir_open", select="none")))
        widgets.append(ui._buttons(ui._button("✉️ Invite selected", "dir_invite", filled=True),
                                   ui._button("← Back", "home")))
    else:
        widgets.append(ui._text("Everyone is already in AIDL. 🎉"))
        widgets.append(ui._buttons(ui._button("← Back", "home")))
    if len(open_) > MAX_ROWS:
        widgets.append(ui._text(f"Showing the first {MAX_ROWS}. Invite the others by email or with a group "
                                "address (e.g. everyone@) in Invite your team."))
    sections = [ui._section(widgets, "Invite from your directory")]
    if joined:
        sections.append(ui._section([ui._text("<br>".join(ui._esc(p["name"] or p["email"]) for p in joined[:50])
                                              + ("<br>…" if len(joined) > 50 else ""))], "Already in AIDL"))
    return ui._card("Your company directory", org.name, sections, notice)
