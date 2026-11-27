"""The Gmail side panel (Google Workspace add-on): Google's signed tokens and the cards.

Google's signing keys are replaced by a test key, so the real token checks run.

    python manage.py test integrations.google_workspace
"""

import json
import time
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core import mail as outbox
from django.test import TestCase, override_settings

from api.models import AIDLUser, Organization

from . import addon_auth

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA = "aidl-addon@gserviceaccount.test"
HOME = "http://backend.test/api/google-workspace/addon/"
ACTION = "http://backend.test/api/google-workspace/addon/action/"


class _Key:
    key = KEY.public_key()


class _Jwks:
    def get_signing_key_from_jwt(self, _token):
        return _Key()


def token(**claims):
    now = int(time.time())
    data = {"iss": "https://accounts.google.com", "iat": now, "exp": now + 300, **claims}
    return jwt.encode(data, KEY, algorithm="RS256")


MESSAGE = "http://backend.test/api/google-workspace/addon/message/"


def event(url=HOME, email="asha@acme.com", sub="1", hd="acme.com", sa=SA, params=None, form=None, system=None,
          gmail=None):
    common = {"parameters": params or {}}
    if form:  # a list = several values (tick boxes)
        common["formInputs"] = {k: {"stringInputs": {"value": v if isinstance(v, list) else [v]}}
                                for k, v in form.items()}
    out = {
        "commonEventObject": common,
        "authorizationEventObject": {
            "systemIdToken": system or token(sub="sa", aud=url, email=sa, email_verified=True),
            "userIdToken": token(sub=sub, aud="addon-client", email=email, email_verified=True, hd=hd,
                                 name="Asha Rao"),
            "userOAuthToken": "user-access-token",
        },
    }
    if gmail:
        out["gmail"] = gmail
    return out


def _resp(status=200, data=None):
    r = mock.Mock(status_code=status, text=json.dumps(data or {}))
    r.json.return_value = data or {}
    return r


def _buttons_of(card):
    return [b for s in card["sections"] for w in s["widgets"] for b in w.get("buttonList", {}).get("buttons", [])]


@override_settings(BACKEND_URL="http://backend.test")
@mock.patch.dict("os.environ", {"GOOGLE_ADDON_SERVICE_ACCOUNT": SA})
@mock.patch.object(addon_auth, "_jwks", _Jwks())
@mock.patch("integrations.google_workspace.google_auth.configured", return_value=True)
@mock.patch("api.card_image.warm")
class GmailAddonTests(TestCase):

    def _home(self, **kw):
        return self.client.post("/api/google-workspace/addon/", json.dumps(event(**kw)),
                                content_type="application/json")

    def _do(self, do, email="asha@acme.com", sub="1", form=None, **params):
        resp = self.client.post("/api/google-workspace/addon/action/",
                                json.dumps(event(ACTION, email=email, sub=sub, params={"do": do, **params}, form=form)),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        action = resp.json()["renderActions"]["action"]
        return action["navigations"][0]["updateCard"], action.get("notification", {}).get("text", "")

    def _card(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["action"]["navigations"][0]["pushCard"]

    def _admin(self):
        """Asha set AIDL up through the website (she's the company's admin)."""
        from .accounts import sign_in
        from .google_auth import GoogleProfile

        sign_in(GoogleProfile(sub="1", email="asha@acme.com", email_verified=True, hd="acme.com",
                              first_name="Asha", last_name="Rao", full_name="Asha Rao", picture=""))

    # ---------- who may call ----------

    def test_requests_need_googles_tokens_for_our_add_on(self, *_):
        self._admin()
        self.assertEqual(self.client.post("/api/google-workspace/addon/", "{}",
                                          content_type="application/json").status_code, 403)
        self.assertEqual(self._home(sa="someone-else@gserviceaccount.test").status_code, 403)
        wrong_url = token(sub="sa", aud="https://evil.test/hook", email=SA, email_verified=True)
        self.assertEqual(self._home(system=wrong_url).status_code, 403)
        expired = jwt.encode({"iss": "https://accounts.google.com", "sub": "sa", "aud": HOME, "email": SA,
                              "email_verified": True, "iat": 1, "exp": 2}, KEY, algorithm="RS256")
        self.assertEqual(self._home(system=expired).status_code, 403)
        self.assertEqual(self._home().status_code, 200)

    def test_switched_off_without_the_service_account_setting(self, *_):
        self._admin()
        with mock.patch.dict("os.environ", {"GOOGLE_ADDON_SERVICE_ACCOUNT": ""}):
            self.assertEqual(self._home().status_code, 403)

    # ---------- cards ----------

    def test_a_new_company_gets_set_up_through_the_website_not_by_opening_the_panel(self, *_):
        card = self._card(self._home())
        self.assertEqual(card["header"]["title"], "Set up AIDL for your company")
        self.assertIn("/api/google-workspace/login/?redirect=1", json.dumps(card))
        self.assertFalse(AIDLUser.objects.exists())
        self.assertFalse(Organization.objects.exists())

    def test_admin_runs_the_admin_center_in_the_side_panel(self, *_):
        self._admin()
        card = self._card(self._home())
        self.assertEqual(card["header"]["title"], "AIDL Admin Center")
        text = json.dumps(card)
        self.assertIn("Get your team started", text)
        self.assertIn(ACTION, text)  # buttons call back into AIDL
        self.assertIn("Your AI policy", text)
        self.assertIn("Your plan", text)
        self.assertIn("See plans", text)
        plans = next(b for s in card["sections"] for w in s["widgets"]
                     for b in w.get("buttonList", {}).get("buttons", []) if "See plans" in b["text"])
        go = plans["onClick"]["openLink"]["url"].split("backend.test", 1)[1]
        self.client.cookies.clear()
        resp = self.client.get(self.client.get(go)["Location"])  # signed link -> the plans page, signed in
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/plans?token=", resp["Location"])

        for item in ("traffic_light", "highway_code"):
            self._do("approve", item=item)
        card, notice = self._do("start")
        self.assertIn("Started", json.dumps(card))
        self.assertIn("🚀 AIDL is running at acme.com", [m.subject for m in outbox.outbox])

        card, notice = self._do("invite", form={"emails": "ben@acme.com, spy@other.com"})
        self.assertIn("Invited 1 colleague", notice)
        self.assertIn("hasn't joined", json.dumps(card, ensure_ascii=False))

    def test_colleague_opens_the_panel_joins_and_gets_their_licence_there(self, *_):
        self._admin()
        for item in ("traffic_light", "highway_code"):
            self._do("approve", item=item)
        self._do("start")

        ben = dict(email="ben@acme.com", sub="2")
        card = self._card(self._home(**ben))  # joins the company as a learner
        self.assertIn("welcome", card["header"]["title"])
        self.assertEqual(AIDLUser.objects.get(email="ben@acme.com").role, "learner")

        card, _ = self._do("start_learning", **ben)
        for _ in range(20):  # the company's policy cards (if any), then the two starter cards
            text = json.dumps(card)
            if '"value": "agree"' in text:
                section = next(s for s in card["sections"] if '"agree"' in json.dumps(s))
                params = section["widgets"][1]["buttonList"]["buttons"][0]["onClick"]["action"]["parameters"]
                p = {x["key"]: x["value"] for x in params if x["key"] != "do"}
                card, _ = self._do("agree", **ben, **p)
            elif '"traffic_light"' in text:
                card, _ = self._do("traffic_light", **ben)
            elif '"highway_code"' in text:
                card, _ = self._do("highway_code", **ben)
            else:
                break
        user = AIDLUser.objects.get(email="ben@acme.com")
        self.assertTrue(user.licence_issued)
        self.assertIn("One last step", json.dumps(card))  # the avatar, then the licence
        user.avatar_data = "iVBORw0KGgo="
        user.save(update_fields=["avatar_data"])
        card, _ = self._do("refresh", **ben)
        self.assertIn("Your licence", json.dumps(card))
        self.assertIn(user.licence_number, json.dumps(card))
        self.assertIn("data:image/jpeg;base64,", json.dumps(card))  # the picture is in the card itself
        self.assertNotIn("emailed", json.dumps(card))
        # the licence lives in the side panel: no licence email
        self.assertNotIn("🎉 Your AI Driving Licence", [m.subject for m in outbox.outbox])

    def test_emails_point_to_the_side_panel_not_a_web_page(self, *_):
        self._admin()
        for item in ("traffic_light", "highway_code"):
            self._do("approve", item=item)
        self._do("start")
        outbox.outbox.clear()
        self._do("invite", form={"emails": "ben@acme.com"})
        invite = outbox.outbox[-1]
        html = invite.alternatives[0][0]
        self.assertIn("AIDL icon in the side panel", html)
        self.assertNotIn("Join with Google", html)  # no sign-in page: the panel knows their work account
        self.assertIn("Join in your browser", html)  # only a small fallback link
        body = html.split("<body", 1)[1]
        self.assertNotIn("acme.com", body)  # no domain Gmail would turn into a link
        self.assertIn("acme.​com uses AIDL", body)

        self._home(email="ben@acme.com", sub="2")  # Ben opens the panel: joined, Welcome email
        welcome = next(m for m in outbox.outbox if m.to == ["ben@acme.com"] and "Welcome" in m.subject)
        html = welcome.alternatives[0][0]
        self.assertIn("AIDL icon in the side panel", html)
        self.assertNotIn("Open AIDL in Gmail", html)  # already in Gmail: no button opening another tab

    def test_agree_to_all_policy_cards_in_one_click(self, *_):
        self._admin()
        for item in ("traffic_light", "highway_code"):
            self._do("approve", item=item)
        self._do("start")
        ben = dict(email="ben@acme.com", sub="2")
        self._home(**ben)
        cards = [{"n": n, "version": f"v{n}", "title": f"Card {n}", "icon": "📜", "image": f"https://img/{n}.png"}
                 for n in (7, 8, 9)]
        real = __import__("integrations.moodle.flow", fromlist=["learner_context"]).learner_context

        def context(user, org):
            return {**real(user, org), "welcome": False, "policy": cards}

        with mock.patch("integrations.moodle.flow.learner_context", side_effect=context):
            card, _ = self._do("refresh", **ben)
            text = json.dumps(card, ensure_ascii=False)
            self.assertIn("I agree to all 3 cards", text)
            self.assertEqual(text.count('"value": "agree"'), 3)  # each card keeps its own button
            button = next(b for s in card["sections"] for w in s["widgets"]
                          for b in w.get("buttonList", {}).get("buttons", []) if "all 3" in b["text"])
            p = {x["key"]: x["value"] for x in button["onClick"]["action"]["parameters"]}

            # card 9 changed after it was shown: it is not agreed to unseen
            def act(user, org, post):
                return "This card was updated while you were reading." if post["n"] == "9" else ""

            with mock.patch("integrations.moodle.flow.learner_action", side_effect=act) as action, \
                    mock.patch("integrations.google_workspace.mail.send_next") as send_next:
                _card, notice = self._do("agree_all", **ben, cards=p["cards"])
            self.assertEqual([(c.args[2]["n"], c.args[2]["version"]) for c in action.call_args_list],
                             [("7", "v7"), ("8", "v8"), ("9", "v9")])
            self.assertIn("agreed to 2 policy cards", notice)
            self.assertIn("1 changed", notice)
            send_next.assert_called()

            cards[1:] = []  # a single card: no "agree to all"
            card, _ = self._do("refresh", **ben)
            self.assertNotIn("agree to all", json.dumps(card, ensure_ascii=False).lower())

    def test_avatar_page_sends_workspace_learners_back_to_gmail(self, *_):
        from api.avatar import _return_url
        from .models import WorkspaceMember

        self._admin()
        self._home(email="ben@acme.com", sub="2")
        ben = AIDLUser.objects.get(email="ben@acme.com")
        WorkspaceMember.objects.get_or_create(user_id=str(ben.pk), defaults={"organization_id": ben.organization_id})
        self.assertEqual(_return_url(ben), "https://mail.google.com/mail/u/?authuser=ben@acme.com")

        WorkspaceMember.objects.filter(user_id=str(ben.pk)).delete()  # e.g. a Google Classroom student
        self.assertEqual(_return_url(ben), "")

    def test_reset_command_starts_the_programme_again(self, *_):
        from django.core.management import call_command

        from api import admin_setup, card_approval

        self._admin()
        for item in ("traffic_light", "highway_code"):
            self._do("approve", item=item)
        self._do("start")
        self._home(email="ben@acme.com", sub="2")
        self._do("start_learning", email="ben@acme.com", sub="2")
        org = Organization.objects.get()

        call_command("reset_workspace_programme", "acme.com")  # without --yes: nothing changes
        self.assertTrue(admin_setup.started(org))
        call_command("reset_workspace_programme", "acme.com", "--yes")
        org.refresh_from_db()
        self.assertFalse(admin_setup.started(org))
        self.assertFalse(card_approval.is_approved(org, "traffic_light"))
        ben = AIDLUser.objects.get(email="ben@acme.com")
        self.assertIsNone(ben.slack_onboarded_at)
        self.assertEqual(AIDLUser.objects.get(email="asha@acme.com").role, "admin")  # accounts stay

    # ---------- policy, directory, own policy file ----------

    def _raw(self, do, email="asha@acme.com", sub="1", form=None, url=ACTION, gmail=None, **params):
        resp = self.client.post(url, json.dumps(event(url, email=email, sub=sub, params={"do": do, **params},
                                                      form=form, gmail=gmail)), content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_admin_chooses_which_policy_topics_the_team_agrees_to(self, *_):
        from api import aup_store
        from api import policy_cards as pc

        self._admin()
        org = Organization.objects.get()  # on the Trial: 3 topics
        text = json.dumps(self._card(self._home()), ensure_ascii=False)
        self.assertIn("Not in your policy", text)
        self.assertIn("🔒 Basic and Enterprise plans", text)

        nav = self._raw("policy_open", n=5)["renderActions"]["action"]["navigations"]
        topic = nav[0]["pushCard"]  # opens on top of the Admin Center
        self.assertIn('"name": "q|5.1"', json.dumps(topic))
        self.assertIn("What your team will read", json.dumps(topic))

        a = aup_store.answers(org, with_tools=False)
        form = {f"q|{q['id']}": a["answers"].get(q["id"]) or q["options"][0]["id"]  # no recommendation: the first
                for q in pc.questions(5, a["answers"])}
        form["q|5.1"] = "5.1.c"  # the admin picks "14 characters or more"
        nav = self._raw("policy_approve", n=5, tools_shown="1", form=form)["renderActions"]["action"]
        if "Please fill in" in nav.get("notification", {}).get("text", ""):  # an answer that needs details
            again = nav["navigations"][0]["updateCard"]  # the same topic, with the details box
            fills = [w["textInput"]["name"] for s in again["sections"] for w in s["widgets"]
                     if w.get("textInput", {}).get("name", "").startswith("f|")]
            self.assertTrue(fills)
            form.update({name: "Bitwarden" for name in fills})
            nav = self._raw("policy_approve", n=5, tools_shown="1", form=form)["renderActions"]["action"]
        self.assertIn("is in your policy", nav["notification"]["text"])
        self.assertEqual(nav["navigations"][0], {"popToRoot": True})
        org.refresh_from_db()
        self.assertIn("14 characters", " ".join(aup_store.card(org, 5)["lines"]))
        self.assertEqual(sorted(aup_store.required_points(org)), [5])  # only what the admin chose

        nav = self._raw("policy_open", n=1)["renderActions"]["action"]  # a paid topic on the Trial
        self.assertIn("Basic and Enterprise", nav["notification"]["text"])
        self.assertIsNone(aup_store.card(org, 1))

        ben = dict(email="ben@acme.com", sub="2")
        self._home(**ben)
        nav = self._raw("policy_approve", n=6, **ben)["renderActions"]["action"]
        self.assertIn("Only your AIDL admins", nav["notification"]["text"])

    def test_admin_invites_people_from_the_company_directory(self, *_):
        self._admin()
        self._home(email="ben@acme.com", sub="2")  # Ben is already in AIDL
        users = {"users": [{"primaryEmail": e, "name": {"fullName": n}} for e, n in (
            ("asha@acme.com", "Asha Rao"), ("ben@acme.com", "Ben Li"), ("carla@acme.com", "Carla Diaz"),
            ("dev@acme.com", "Dev Shah"))] + [{"primaryEmail": "old@acme.com", "suspended": True}]}
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, users)) as get:
            nav = self._raw("dir_open")["renderActions"]["action"]["navigations"]
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer user-access-token")
        self.assertEqual(get.call_args.kwargs["params"]["domain"], "acme.com")
        card = nav[0]["pushCard"]
        boxes = next(w["selectionInput"] for s in card["sections"] for w in s["widgets"] if "selectionInput" in w)
        self.assertEqual([(i["value"], i["selected"]) for i in boxes["items"]],
                         [("carla@acme.com", True), ("dev@acme.com", True)])  # not Asha, Ben (joined), old
        self.assertIn("Ben Li", json.dumps(card))  # listed as already in AIDL

        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, users)):
            nav = self._raw("dir_open", select="none")["renderActions"]["action"]["navigations"]
        boxes = next(w["selectionInput"] for s in nav[0]["updateCard"]["sections"] for w in s["widgets"]
                     if "selectionInput" in w)
        self.assertFalse(any(i["selected"] for i in boxes["items"]))

        outbox.outbox.clear()
        nav = self._raw("dir_invite", form={"people": ["carla@acme.com", "dev@acme.com"]})["renderActions"]["action"]
        self.assertIn("Invited 2 colleagues", nav["notification"]["text"])
        self.assertEqual(sorted(m.to[0] for m in outbox.outbox), ["carla@acme.com", "dev@acme.com"])

    def test_admin_invites_everyone_in_the_directory_with_one_click(self, *_):
        self._admin()
        self._home(email="ben@acme.com", sub="2")  # Ben is already in AIDL
        users = {"users": [{"primaryEmail": e} for e in ("ben@acme.com", "carla@acme.com", "dev@acme.com")]}
        self.assertTrue(any(b["text"].startswith("✉️ Invite everyone") for b in _buttons_of(self._card(self._home()))))
        outbox.outbox.clear()
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, users)):
            nav = self._raw("dir_invite_all")["renderActions"]["action"]
        self.assertIn("Invited 2 colleagues", nav["notification"]["text"])
        self.assertEqual(sorted(m.to[0] for m in outbox.outbox), ["carla@acme.com", "dev@acme.com"])
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, users)):
            nav = self._raw("dir_invite_all")["renderActions"]["action"]
        self.assertIn("already in AIDL or has an invite", nav["notification"]["text"])  # no second email

    def test_directory_not_allowed_says_how_to_fix_it(self, *_):
        self._admin()
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(403)):
            card = self._raw("dir_open")["renderActions"]["action"]["navigations"][0]["pushCard"]
        self.assertIn("Contact sharing", json.dumps(card))
        scope = _resp(403, {"error": {"message": "Request had insufficient authentication scopes."}})
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=scope):
            resp = self._raw("dir_open")
        self.assertEqual(resp, {"requesting_google_scopes": {
            "scopes": ["https://www.googleapis.com/auth/admin.directory.user.readonly"]}})  # Google asks to allow

    def test_admin_turns_a_policy_file_attached_to_an_email_into_cards(self, *_):
        self._admin()
        Organization.objects.update(plan="enterprise")
        gmail = {"messageId": "m1", "accessToken": "msg-token"}
        msg = {"payload": {"parts": [{"filename": "", "body": {}},
                                     {"filename": "Acme AI policy.pdf", "body": {"attachmentId": "a1", "size": 900}},
                                     {"filename": "photo.png", "body": {"attachmentId": "a2"}}]}}
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, msg)) as get:
            resp = self.client.post(MESSAGE, json.dumps(event(MESSAGE, gmail=gmail)), content_type="application/json")
        card = self._card(resp)
        self.assertEqual(get.call_args.kwargs["headers"]["X-Goog-Gmail-Access-Token"], "msg-token")
        offer = next(b for b in _buttons_of(card) if "Use this file" in b["text"])
        p = {x["key"]: x["value"] for x in offer["onClick"]["action"]["parameters"]}
        self.assertEqual((p["att"], p["name"]), ("a1", "Acme AI policy.pdf"))  # the PDF, not the photo

        import base64

        pdf = {"data": base64.urlsafe_b64encode(b"%PDF-1.4 policy").decode()}
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, pdf)), \
                mock.patch("api.aup_upload.start") as start:
            nav = self._raw("policy_upload", gmail=gmail, att="a1", name="Acme AI policy.pdf")
        self.assertEqual(start.call_args.args[2:], ("Acme AI policy.pdf", b"%PDF-1.4 policy"))
        self.assertIn("reading your policy", nav["renderActions"]["action"]["notification"]["text"])

        Organization.objects.update(plan="trial")  # the Trial can't upload: no offer
        with mock.patch("integrations.google_workspace.google_api.requests.get", return_value=_resp(200, msg)):
            resp = self.client.post(MESSAGE, json.dumps(event(MESSAGE, gmail=gmail)), content_type="application/json")
        self.assertFalse([b for b in _buttons_of(self._card(resp)) if "Use this file" in b["text"]])

    def test_learners_cant_use_admin_buttons(self, *_):
        self._admin()
        ben = dict(email="ben@acme.com", sub="2")
        self._home(**ben)
        _card, notice = self._do("start", **ben)
        self.assertIn("Only", notice)
        from api import admin_setup

        self.assertFalse(admin_setup.started(Organization.objects.get()))

    def test_emails_open_gmail_when_the_side_panel_is_on(self, *_):
        self._admin()
        self._do("approve", item="traffic_light")
        self._do("approve", item="highway_code")
        self._do("start")
        started = next(m for m in outbox.outbox if m.subject.startswith("🚀"))
        self.assertNotIn("https://mail.google.com/", started.body)  # already in Gmail: no "open Gmail" button
        self.assertIn("AIDL icon in the side panel", started.body)
        self.assertIn("/api/google-workspace/go/?t=", started.body)  # the web page, as a fallback
