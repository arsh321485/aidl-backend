"""Installing the AIDL Teams app + its channel tab after an admin signs in (teams_app_install)."""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import admin_setup, teams_app_install, teams_channel
from .models import TeamsPost
from .teams_portal import COOKIE, _session_token
from .tests import make_org, make_user

APP_ID = "aa5e1a12-7bda-4b2d-8abb-b0d0ad1dc2d2"
CATALOG_ID = "catalog-77"
TEAM, CHANNEL = "team-1", "chan-1"


class FakeGraph:
    """Just enough of Microsoft Graph, with state, to see what AIDL creates."""

    def __init__(self, *, teams_admin=True):
        self.teams_admin = teams_admin
        self.catalog: list[dict] = []
        self.installed: list[str] = []
        self.tabs = [{"id": "web-1", "configuration": {"entityId": "aidl_app"},
                      "teamsApp": {"id": "com.microsoft.teamspace.tab.web"}}]
        self.cards: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.install_status = 201
        self.old_app = False  # the installed copy predates the channel tab (no tab support)
        self.tab_failures = 0  # answer the first n tab requests with 400 (app not ready yet)

    class R:
        def __init__(self, status, body=None):
            self.status_code, self._body, self.headers = status, body or {}, {}
            self.content = json.dumps(self._body).encode()
            self.text = self.content.decode()

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests

                raise requests.HTTPError(str(self.status_code), response=self)

    def get(self, url, headers=None, params=None, timeout=None, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/appCatalogs/teamsApps"):
            return self.R(200, {"value": self.catalog})
        if url.endswith(f"/teams/{TEAM}/installedApps"):
            return self.R(200, {"value": [{"id": "inst-" + i, "teamsApp": {"id": i},
                                           "teamsAppDefinition": {"version": "1.0.0" if self.old_app else "2.0.0"}}
                                          for i in self.installed]})
        if url.endswith(f"/channels/{CHANNEL}/tabs"):
            return self.R(200, {"value": self.tabs})
        return self.R(404)

    def post(self, url, headers=None, json=None, data=None, timeout=None, **kw):
        self.calls.append(("POST", url))
        if url.endswith("/appDefinitions"):
            if "requiresReview=true" not in url and not self.teams_admin:
                return self.R(403, {"error": {"code": "Forbidden"}})
            state = "submitted" if "requiresReview=true" in url else "published"
            self.catalog[0]["appDefinitions"].append({"publishingState": state, "version": "2.0.0"})
            return self.R(201)
        if url.endswith("/upgrade"):
            self.old_app = False
            return self.R(204)
        if "/appCatalogs/teamsApps" in url:
            review = "requiresReview=true" in url
            if not review and not self.teams_admin:
                return self.R(403, {"error": {"code": "Forbidden"}})
            state = "submitted" if review else "published"
            app = {"id": CATALOG_ID, "externalId": APP_ID, "appDefinitions": [{"publishingState": state}]}
            self.catalog.append(app)
            return self.R(201, {"id": CATALOG_ID, "externalId": APP_ID})
        if url.endswith(f"/teams/{TEAM}/installedApps"):
            if self.install_status >= 400:
                return self.R(self.install_status, {"error": {"code": "Forbidden"}})
            self.installed.append(json["teamsApp@odata.bind"].rsplit("/", 1)[1])
            return self.R(201)
        if url.endswith(f"/channels/{CHANNEL}/tabs"):
            app = json["teamsApp@odata.bind"].rsplit("/", 1)[1]
            if app not in self.installed:
                return self.R(404)
            if self.old_app:
                return self.R(400, {"error": {"code": "BadRequest", "message": "The specified app does not support tabs."}})
            if self.tab_failures > 0:
                self.tab_failures -= 1
                return self.R(400, {"error": {"code": "BadRequest", "message": "app not ready"}})
            self.tabs.append({"id": f"tab-{len(self.tabs)}", "configuration": json["configuration"],
                              "teamsApp": {"id": app}})
            return self.R(201, self.tabs[-1])
        if url.endswith(f"/channels/{CHANNEL}/messages"):
            self.cards.append(json)
            return self.R(201, {"id": f"msg-{len(self.cards)}"})
        return self.R(404)

    def delete(self, url, headers=None, timeout=None, **kw):
        self.calls.append(("DELETE", url))
        self.tabs = [t for t in self.tabs if not url.endswith("/tabs/" + t["id"])]
        return self.R(204)

    def writes(self, part: str) -> int:
        return sum(1 for method, url in self.calls if method == "POST" and part in url)

    def button(self) -> str:
        card = __import__("json").loads(self.cards[-1]["attachments"][0]["content"])
        return card["actions"][0]["url"]


@override_settings(TEAMS_AUTO_INSTALL=True, TEAMS_CHANNEL_TAB=True, TEAMS_APP_ID=APP_ID, SLACK_REPLY_SYNC=True)
class TeamsAppInstallTests(TestCase):
    def setUp(self):
        self.org = make_org(name="Secureitlab", teams_team_id=TEAM, ms_tenant_id="tenant-1",
                            teams_welcome_channel_id=CHANNEL, teams_welcome_message_id="old-card")
        self.admin = make_user(self.org, full_name="Asha Admin", ms_refresh_token="x")
        self.org.owner_user_id = str(self.admin.pk)
        self.org.save(update_fields=["owner_user_id"])
        self.graph = FakeGraph()
        for target, kwargs in (("requests.get", {"side_effect": lambda *a, **k: self.graph.get(*a, **k)}),
                               ("requests.post", {"side_effect": lambda *a, **k: self.graph.post(*a, **k)}),
                               ("requests.delete", {"side_effect": lambda *a, **k: self.graph.delete(*a, **k)}),
                               ("api.teams_app_install.install_token", {"return_value": "install-tok"}),
                               ("api.teams_channel.token_for", {"return_value": "tok"}),
                               ("api.teams_messaging.delete_channel_message", {"return_value": True})):
            p = patch(target, **kwargs)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    def _status(self):
        self.org.refresh_from_db()
        return teams_app_install.progress(self.org).get("status")

    # ---------- a Teams / Global admin signs in ----------

    def test_a_teams_admin_gets_everything_at_once(self):
        self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual(self.graph.installed, [CATALOG_ID])
        entities = [t["configuration"]["entityId"] for t in self.graph.tabs]
        self.assertEqual(entities, ["aidl_tab"])  # AIDL's Website tab is gone
        self.assertTrue(self.graph.tabs[0]["configuration"]["contentUrl"].endswith("/api/teams/app/tab/"))
        self.assertEqual(len(self.graph.cards), 1)
        self.assertIn(f"/l/entity/{CATALOG_ID}/aidl_tab?", self.graph.button())  # the catalog's id, not the manifest's

    def test_signing_in_again_creates_nothing_twice(self):
        teams_app_install.run(self.org)
        self.org.refresh_from_db()
        before = list(self.graph.calls)
        for _ in range(3):
            self.org.refresh_from_db()
            self.assertEqual(teams_app_install.run(self.org), "installed")
        new_posts = [c for c in self.graph.calls[len(before):] if c[0] == "POST"]
        self.assertEqual(new_posts, [])
        self.assertEqual((len(self.graph.catalog), len(self.graph.installed), len(self.graph.cards)), (1, 1, 1))

    def test_an_app_already_in_the_catalog_is_not_published_again(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "published"}]})
        self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps"), 0)
        self.assertEqual(self.graph.installed, [CATALOG_ID])

    def test_already_installed_only_adds_the_tab(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "published"}]})
        self.graph.installed.append(CATALOG_ID)
        teams_app_install.run(self.org)
        self.assertEqual(self.graph.writes("/installedApps"), 0)
        self.assertEqual(self.graph.writes("/tabs"), 1)

    def test_a_tab_added_by_hand_just_gets_the_card_link(self):
        self.graph.tabs.append({"id": "t2", "configuration": {"entityId": "aidl_tab"}, "teamsApp": {"id": APP_ID}})
        self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps") + self.graph.writes("/installedApps"), 0)
        self.assertIn(f"/l/entity/{APP_ID}/aidl_tab?", self.graph.button())

    def test_a_tab_refused_while_the_app_settles_is_tried_again(self):
        self.graph.tab_failures = 2
        with patch("api.teams_app_install.time.sleep") as sleep:
            self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([t["configuration"]["entityId"] for t in self.graph.tabs], ["aidl_tab"])

    def test_a_tab_that_never_works_says_why_and_keeps_the_old_link(self):
        self.graph.tab_failures = 99
        with patch("api.teams_app_install.time.sleep"):
            self.assertEqual(teams_app_install.run(self.org), "failed")
        self.assertIn("400 BadRequest: app not ready", teams_app_install.progress(self.org)["error"])
        self.assertEqual(self.graph.cards, [])  # the card keeps its link
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps"), 1)
        # the next run only retries the tab: nothing published or installed twice
        self.org.refresh_from_db()
        self.graph.tab_failures = 0
        self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual((len(self.graph.catalog), len(self.graph.installed)), (1, 1))

    def test_an_old_copy_in_the_catalog_is_updated_and_the_install_upgraded(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "published", "version": "1.0.0"}]})
        self.graph.installed.append(CATALOG_ID)
        self.graph.old_app = True
        self.assertEqual(teams_app_install.run(self.org), "installed")
        self.assertEqual(self.graph.writes("/appDefinitions"), 1)
        self.assertEqual(self.graph.writes("/upgrade"), 1)
        self.assertEqual([t["configuration"]["entityId"] for t in self.graph.tabs], ["aidl_tab"])

    def test_an_up_to_date_catalog_is_left_alone(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "published", "version": "2.0.0"}]})
        self.graph.installed.append(CATALOG_ID)
        teams_app_install.run(self.org)
        self.assertEqual(self.graph.writes("/appDefinitions") + self.graph.writes("/upgrade"), 0)

    def test_a_non_admin_submits_the_new_version_for_approval(self):
        self.graph.teams_admin = False
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "published", "version": "1.0.0"}]})
        self.assertEqual(teams_app_install.run(self.org), "submitted")
        self.assertEqual(self.graph.writes("/appDefinitions"), 2)

    # ---------- someone without Teams admin rights signs in ----------

    def test_others_submit_it_for_approval_and_keep_the_old_link(self):
        self.graph.teams_admin = False
        self.assertEqual(teams_app_install.run(self.org), "submitted")
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps"), 2)  # straight publish refused → review
        self.assertEqual((self.graph.installed, self.graph.cards), ([], []))  # card untouched
        self.assertEqual([t["configuration"]["entityId"] for t in self.graph.tabs], ["aidl_app"])

    def test_waiting_for_approval_is_not_submitted_twice(self):
        self.graph.teams_admin = False
        teams_app_install.run(self.org)
        self.org.refresh_from_db()
        self.assertEqual(teams_app_install.run(self.org), "submitted")
        self.assertEqual(len(self.graph.catalog), 1)
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps"), 2)

    def test_once_approved_the_hourly_job_finishes_it(self):
        self.graph.teams_admin = False
        teams_app_install.run(self.org)
        self.org.refresh_from_db()
        teams_app_install.run_if_due(self.org)  # checked at most hourly
        self.assertEqual(self.graph.installed, [])
        self.graph.catalog[0]["appDefinitions"][0]["publishingState"] = "published"  # their Teams admin approves
        p = teams_app_install.progress(self.org)
        admin_setup.update(self.org, teams_app={**p, "at": "2000-01-01T00:00:00+00:00"})
        with patch("api.teams_channel.refresh_home_link"):
            teams_channel.ensure_home(self.org)
        self.assertEqual(self._status(), "installed")
        self.assertEqual(len(self.graph.cards), 1)
        self.assertIn("/aidl_tab?", self.graph.button())

    def test_a_rejection_is_not_retried_every_hour(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID,
                                   "appDefinitions": [{"publishingState": "rejected"}]})
        self.assertEqual(teams_app_install.run(self.org), "rejected")
        self.org.refresh_from_db()
        self.assertFalse(teams_app_install.due(self.org))

    def test_the_admin_center_says_what_is_happening(self):
        self.graph.teams_admin = False
        teams_app_install.run(self.org)
        client = APIClient()
        client.cookies[COOKIE] = _session_token(self.admin)
        with patch("api.teams_channel.dispatch_if_due"):
            resp = client.get("/api/teams/app/admin/")
        self.assertContains(resp, "AIDL is waiting for your Microsoft Teams administrator's approval.")
        self.assertContains(resp, "Pending approval")
        self.assertContains(resp, "Teams admin center")
        self.assertContains(resp, "/api/teams/app/install/status/")  # it checks by itself
        self.assertNotContains(resp, "AIDL is installed in Teams")

    def _admin_center(self):
        client = APIClient()
        client.cookies[COOKIE] = _session_token(self.admin)
        with patch("api.teams_channel.dispatch_if_due"):
            return client, client.get("/api/teams/app/admin/")

    def _approve(self):
        for app in self.graph.catalog:
            for d in app["appDefinitions"]:
                d["publishingState"] = "published"

    def test_a_normal_admin_waits_then_it_finishes_by_itself(self):
        self.graph.teams_admin = False
        teams_app_install.run(self.org)
        client, _ = self._admin_center()
        # still waiting: the page's check changes nothing
        self.org.refresh_from_db()
        with patch("api.teams_app_install.CHECK_EVERY", teams_app_install.timedelta(0)):
            self.assertEqual(client.get("/api/teams/app/install/status/").json(), {"status": "submitted"})
        self.assertEqual((self.graph.installed, self.graph.cards), ([], []))
        # their Teams admin approves; the next check installs, adds the tab, updates the card
        self._approve()
        with patch("api.teams_app_install.CHECK_EVERY", teams_app_install.timedelta(0)):
            self.assertEqual(client.get("/api/teams/app/install/status/").json(), {"status": "installed"})
        self.assertEqual(self.graph.installed, [CATALOG_ID])
        self.assertEqual([t["configuration"]["entityId"] for t in self.graph.tabs], ["aidl_tab"])
        self.assertIn(f"/l/entity/{CATALOG_ID}/aidl_tab?", self.graph.button())
        _, resp = self._admin_center()
        self.assertContains(resp, "AIDL is installed in Teams")
        self.assertNotContains(resp, "waiting for your Microsoft Teams administrator")

    def test_the_check_is_not_run_more_often_than_every_45_seconds(self):
        self.graph.teams_admin = False
        teams_app_install.run(self.org)
        self.org.refresh_from_db()
        before = len(self.graph.calls)
        self.assertEqual(teams_app_install.check_now(self.org), "submitted")
        self.assertEqual(len(self.graph.calls), before)

    def test_a_new_version_waiting_for_approval_stays_submitted_each_hour(self):
        self.graph.catalog.append({"id": CATALOG_ID, "externalId": APP_ID, "appDefinitions": [
            {"publishingState": "published", "version": "1.0.0"},
            {"publishingState": "submitted", "version": "2.0.0"}]})
        self.graph.installed.append(CATALOG_ID)
        self.assertEqual(teams_app_install.run(self.org), "submitted")
        self.assertEqual(self.graph.writes("/appDefinitions") + self.graph.writes("/upgrade"), 0)

    def test_a_teams_admin_sees_success_and_no_pending_message(self):
        teams_app_install.run(self.org)
        client, resp = self._admin_center()
        self.assertContains(resp, "AIDL is installed in Teams")
        self.assertNotContains(resp, "waiting for your Microsoft Teams administrator")
        self.assertNotContains(resp, "/api/teams/app/install/status/")
        self.assertEqual(client.get("/api/teams/app/install/status/").json(), {"status": "installed"})

    # ---------- things that stop it ----------

    def test_no_admin_login_with_the_permissions(self):
        self.install_token.return_value = ""
        self.assertEqual(teams_app_install.run(self.org), "needs_consent")
        self.assertEqual([c for c in self.graph.calls if c[0] == "POST"], [])

    def test_custom_apps_turned_off(self):
        self.graph.install_status = 403
        self.assertEqual(teams_app_install.run(self.org), "blocked")
        self.assertEqual(self.graph.cards, [])
        self.assertIn("403", teams_app_install.progress(self.org)["error"])

    def test_an_unreadable_catalog_never_publishes_blind(self):
        real = self.graph.get

        def no_catalog(url, **kw):
            return FakeGraph.R(500) if url.endswith("/appCatalogs/teamsApps") else real(url, **kw)
        self.requests_get = patch("requests.get", side_effect=no_catalog).start()
        self.addCleanup(patch.stopall)
        self.assertEqual(teams_app_install.run(self.org), "failed")
        self.assertEqual(self.graph.writes("/appCatalogs/teamsApps"), 0)

    def test_two_runs_at_once_do_not_both_publish(self):
        TeamsPost.objects.create(organization_id=str(self.org.pk), key=teams_app_install.LOCK_KEY)
        teams_app_install.run(self.org)
        self.assertEqual([c for c in self.graph.calls if c[0] == "POST"], [])

    @override_settings(TEAMS_AUTO_INSTALL=False)
    def test_switched_off_does_nothing(self):
        self.assertEqual(teams_app_install.run(self.org), "")
        self.assertEqual(self.graph.calls, [])

    def test_slack_organisations_are_left_alone(self):
        slack = make_org(name="Slacky", slack_team_id="T123", teams_team_id=TEAM, teams_welcome_channel_id=CHANNEL)
        self.assertFalse(teams_app_install.enabled(slack))
        self.assertEqual(teams_app_install.run(slack), "")


class SignInScopesTests(TestCase):
    @override_settings(TEAMS_AUTO_INSTALL=False)
    def test_off_asks_for_what_it_always_did(self):
        from django.conf import settings

        from .microsoft_auth import signin_scopes

        self.assertEqual(signin_scopes(), list(settings.MS_SCOPES))

    @override_settings(TEAMS_AUTO_INSTALL=True)
    def test_on_also_asks_for_the_install_permissions(self):
        from .microsoft_auth import signin_scopes

        scopes = signin_scopes()
        for s in ("AppCatalog.ReadWrite.All", "AppCatalog.Submit", "TeamsAppInstallation.ReadWriteForTeam",
                  "TeamsTab.Create"):
            self.assertEqual(scopes.count(s), 1)

    def test_everyday_tokens_keep_the_usual_permissions(self):
        from django.conf import settings

        from . import microsoft_auth

        with patch.object(microsoft_auth, "_msal_app") as app:
            microsoft_auth.refresh_graph_token("rt")
            self.assertEqual(app.return_value.acquire_token_by_refresh_token.call_args.kwargs["scopes"],
                             settings.MS_SCOPES)
            microsoft_auth.refresh_graph_token("rt", settings.TEAMS_INSTALL_SCOPES)
            self.assertEqual(app.return_value.acquire_token_by_refresh_token.call_args.kwargs["scopes"],
                             settings.TEAMS_INSTALL_SCOPES)
