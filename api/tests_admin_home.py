"""The guided Admin Center on the Slack app's Home tab (admin_home.py)."""

import json

from django.test import TestCase

from .models import AIDLUser, Organization
from .tests import SlackOnboardingTests, _answer_policy, _approve_all, make_org, make_user


class AdminHomeTests(TestCase):
    """Drawn per admin with their own permissions, kept current after joins / leaves, and the shared #aidl
    message turns into a short post once the Home tab is in use."""

    SECRET = "shh"
    _signed = SlackOnboardingTests._signed

    def setUp(self):
        from . import admin_home
        from .slack_client import encrypt_token

        admin_home._OPENED.clear()
        self.org = make_org(name="Northwind Logistics", slack_team_id="T9", slack_channel_id="C42",
                            slack_bot_user_id="UBOT", slack_bot_token=encrypt_token("xoxb-t"), plan="trial",
                            slack_home_message_ts="5.5")
        self.master = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        self.calls = []

    def _fake(self, method, token, *, json=None, params=None):
        self.calls.append((method, json or params))
        if method == "users.info":
            uid = (params or {}).get("user")
            return {"ok": True, "user": {"id": uid, "tz_offset": 0, "profile": {
                "email": f"{uid.lower()}@acme.example", "real_name": f"Person {uid}"}}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D7"}}
        if method in ("views.open", "views.push", "views.update"):
            return {"ok": True, "view": {"id": "V1"}}
        return {"ok": True, "ts": "1.1"}

    def _open_home(self, user="U1"):
        body = json.dumps({"type": "event_callback", "team_id": "T9", "api_app_id": "A777",
                           "event": {"type": "app_home_opened", "user": user, "tab": "home"}})
        return self._signed("/api/slack/events/", body, "application/json")

    def _event(self, user, kind="member_joined_channel"):
        body = json.dumps({"type": "event_callback", "team_id": "T9",
                           "event": {"type": kind, "user": user, "channel": "C42"}})
        return self._signed("/api/slack/events/", body, "application/json")

    def _home_click(self, user, action):
        from urllib.parse import urlencode

        payload = {"type": "block_actions", "team": {"id": "T9"}, "user": {"id": user, "team_id": "T9"},
                   "trigger_id": "TR", "container": {"type": "view", "view_id": "VH"},
                   "view": {"id": "VH", "type": "home", "callback_id": "aidl_home"}, "actions": [action]}
        return self._signed("/api/slack/interactions/", urlencode({"payload": json.dumps(payload)}),
                            "application/x-www-form-urlencoded")

    def _published(self, user="U1") -> list:
        return [b["view"]["blocks"] for m, b in self.calls if m == "views.publish" and b["user_id"] == user]

    def _channel_posts(self) -> list:
        return [b for m, b in self.calls if m in ("chat.update", "chat.postMessage") and b.get("channel") == "C42"]

    @staticmethod
    def _action_ids(blocks) -> list:
        ids = []
        for b in blocks:
            for e in (b.get("elements") or []) + ([b["accessory"]] if b.get("accessory") else []):
                if e.get("action_id"):
                    ids.append(e["action_id"])
                for o in e.get("options") or []:
                    ids.append(o["value"].split("|")[0])
        return ids

    def test_master_admin_home_has_banner_welcome_overview_and_next_step(self):
        from . import admin_home

        self._open_home()
        blocks = self._published()[-1]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertTrue(blocks[0]["image_url"].endswith(f"/static/aidl/admin-banner-{admin_home.BANNER_VERSION}.png"))
        self.assertEqual(blocks[1]["text"]["text"], "Welcome, Priya")
        text = json.dumps(blocks)
        self.assertIn("Northwind Logistics", text)
        self.assertIn("as the master admin", text)
        self.assertIn("*Plan*\\nTrial", text)
        self.assertIn("0 of 5 seats", text)
        self.assertIn("0 of 5 steps", text)
        self.assertIn("*Your next step*\\n*1. Your AI policy*", text)
        self.assertIn("aidl_policy_open", self._action_ids(blocks))
        self.assertIn("aidl_upgrade", self._action_ids(blocks))  # the master may change the plan
        for n, title in admin_home.STEP_TITLES.items():  # every step is listed
            self.assertIn(f"{n}. {title}", text)
        self.assertLessEqual(len(blocks), admin_home.MAX_BLOCKS)

    def test_first_admin_visit_switches_the_channel_message_to_the_short_post(self):
        from . import admin_setup

        self._open_home()
        st = admin_setup.state(Organization.objects.get(pk=self.org.pk))
        self.assertTrue(st.get("app_home_at"))
        self.assertEqual(st.get("slack_app_id"), "A777")
        post = self._channel_posts()[-1]
        text = json.dumps(post["blocks"])
        self.assertIn("AI Driving Licence channel for Northwind Logistics", text)
        self.assertNotIn("aidl_policy_open", text)  # no admin buttons in the shared channel
        self.assertEqual(post["ts"], "5.5")  # the same message is updated, not a new one

    def test_restricted_admin_sees_only_what_their_permissions_allow(self):
        make_user(self.org, microsoft_id="slack:T9:U5", full_name="Sam Okafor", perm_approve_apps=False,
                  perm_access_cards=False, perm_create_card=False, perm_approve_cards=True)
        self._open_home("U5")
        blocks = self._published("U5")[-1]
        text = json.dumps(blocks)
        self.assertEqual(blocks[1]["text"]["text"], "Welcome, Sam")
        self.assertIn("an admin who can approve cards", text)
        self.assertIn("Priya Shah", text)  # who can do step 1 instead
        ids = self._action_ids(blocks)
        self.assertNotIn("aidl_policy_open", ids)
        self.assertNotIn("aidl_upgrade", ids)

    def test_restricted_admin_is_refused_on_the_server_even_from_the_home_tab(self):
        make_user(self.org, microsoft_id="slack:T9:U5", full_name="Sam Okafor", perm_approve_apps=False,
                  perm_access_cards=False, perm_create_card=False, perm_approve_cards=True)
        self._home_click("U5", {"action_id": "aidl_add_people", "value": "team"})
        shown = json.dumps([b for m, b in self.calls if m in ("views.open", "views.update")])
        self.assertIn("permissions don't include this", shown)
        self.assertNotIn("aidl_add_people", shown)

    def test_learner_and_stranger_get_their_own_home(self):
        from . import admin_setup

        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U2", full_name="Jordan Ellis")
        self._open_home("U2")
        learner = json.dumps(self._published("U2")[-1])
        self.assertIn("Your AI Driving Licence", learner)
        self.assertIn("aidl_employee_menu", learner)
        self.assertNotIn("aidl_policy_open", learner)
        self._open_home("U9")
        self.assertIn("isn't set up for you", json.dumps(self._published("U9")[-1]))
        # Only an admin's visit switches the channel message.
        self.assertFalse(admin_setup.state(Organization.objects.get(pk=self.org.pk)).get("app_home_at"))

    def test_joins_and_leaves_redraw_the_admin_center(self):
        self._open_home()
        self.calls.clear()
        self._event("U2")
        self.assertIn("1 of 5 seats", json.dumps(self._published()[-1]))
        self.assertTrue(self._channel_posts())
        self.calls.clear()
        self._event("U2", "member_left_channel")
        self.assertIn("0 of 5 seats", json.dumps(self._published()[-1]))

    def test_joins_redraw_the_channel_admin_center_before_the_home_tab_is_used(self):
        self._event("U2")
        post = self._channel_posts()[-1]
        self.assertIn("AIDL Admin Center", json.dumps(post["blocks"]))  # the full one, as before
        self.assertFalse(self._published())

    def test_home_clicks_open_popups_and_reply_by_direct_message(self):
        self._home_click("U1", {"action_id": "aidl_add_people", "value": "team"})
        self.assertTrue(any(m == "views.open" for m, _b in self.calls))
        self.assertFalse(any(m == "views.push" for m, _b in self.calls))
        self.calls.clear()
        with self.settings(FRONTEND_URL="https://aidl.example"):
            self._home_click("U1", {"action_id": "aidl_upgrade", "value": "upgrade"})
        dm = [b for m, b in self.calls if m == "chat.postMessage" and b["channel"] == "U1"]
        self.assertIn("/plans?token=", json.dumps(dm[-1]["blocks"]))

    def test_more_menu_stands_for_the_button_it_names(self):
        self._home_click("U1", {"action_id": "aidl_home_more", "type": "overflow",
                                "selected_option": {"value": "aidl_team_manage|team"}})
        shown = json.dumps([b for m, b in self.calls if m in ("views.open", "views.update")])
        self.assertIn("Remove people", shown)

    def test_running_programme_shows_numbers_and_a_confirmed_start(self):
        from . import admin_setup

        _answer_policy(self.org)
        _approve_all(self.org)
        admin_setup.update(Organization.objects.get(pk=self.org.pk), admins_step="skipped")
        member = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U2", full_name="Jordan Ellis")
        self._open_home()
        blocks = self._published()[-1]
        start = next(e for b in blocks for e in b.get("elements") or [] if e.get("action_id") == "aidl_send_item")
        self.assertIn("confirm", start)
        self.assertIn("Start for 1 person", start["text"]["text"])
        admin_setup.update(Organization.objects.get(pk=self.org.pk), flow_started="2026-11-01T10:00:00")
        member.licence_issued = True
        member.save()
        self.calls.clear()
        from . import admin_home

        admin_home._OPENED.clear()
        self._open_home()
        text = json.dumps(self._published()[-1])
        self.assertIn("*Your programme*", text)
        self.assertIn("*Licences issued*\\n1 of 1 person", text)
        self.assertIn("5 of 5 steps", text)
        self.assertNotIn("*Your next step*", text)
        self.assertIn("Welcome back, Priya", text)

    def test_half_approved_onboarding_says_what_is_missing_and_start_stays_locked(self):
        from . import admin_setup, card_approval

        _answer_policy(self.org)
        admin_setup.update(Organization.objects.get(pk=self.org.pk), admins_step="skipped")
        make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U2", full_name="Jordan Ellis")
        card_approval.approve_onboarding(self.org, "traffic_light", None)  # the Highway Code is still waiting
        self._open_home()
        blocks = self._published()[-1]
        text = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("✅ 🚦 Traffic Light Check", text)
        self.assertIn("⏳ 📖 The Highway Code", text)
        self.assertIn("opens after approving the Traffic Light Check and the Highway Code", text)
        self.assertNotIn("aidl_send_item", self._action_ids(blocks))
        card_approval.approve_onboarding(self.org, "highway_code", None)  # now Start is the next step
        self.calls.clear()
        from . import admin_home

        admin_home._OPENED.clear()
        self._open_home()
        blocks = self._published()[-1]
        self.assertIn("aidl_send_item", self._action_ids(blocks))
        self.assertIn("*Your next step*\\n*5. Start the programme*", json.dumps(blocks))

    def test_reset_keeps_the_home_tab_switch(self):
        from . import admin_setup

        self._open_home()
        admin_setup.reset(Organization.objects.get(pk=self.org.pk), fresh=True)
        st = admin_setup.state(Organization.objects.get(pk=self.org.pk))
        self.assertTrue(st.get("app_home_at"))
        self.assertFalse(st.get("flow_started"))

    def test_banner_file_is_served(self):
        from . import admin_home

        resp = self.client.get(f"/static/aidl/admin-banner-{admin_home.BANNER_VERSION}.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(self.client.get("/static/aidl/admin-banner-nope.png").status_code, 404)
