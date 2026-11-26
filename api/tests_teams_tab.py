"""Open AIDL opens the channel's AIDL tab, inside Teams (not a popup)."""

import json
from unittest.mock import patch

import jwt
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import admin_setup, teams_channel, teams_channel_tabs
from .models import AIDLUser
from .teams_portal import COOKIE, _handoff_code, _session_token
from .tests import make_org, make_user

TAB = "https://teams.microsoft.com/l/entity/com.microsoft.teamspace.tab.web/aidl_app?context=x"


class _Posts:
    def setUp(self):
        self.org = make_org(name="Secureitlab", teams_team_id="team-1", ms_tenant_id="t1",
                            teams_welcome_channel_id="chan-1")
        self.admin = make_user(self.org, full_name="Asha Admin", ms_refresh_token="x")
        self.posts = []

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            self.posts.append(json)

            class R:
                status_code, content, headers = 201, b"{}", {}

                def json(self_inner):
                    return {"id": f"msg-{len(self.posts)}"}
            return R()

        for target, kwargs in (("api.teams_channel.token_for", {"return_value": "tok"}),
                               ("api.teams_channel.requests.post", {"side_effect": fake_post}),
                               ("api.teams_messaging.delete_channel_message", {"return_value": True})):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def _button(self, n=-1) -> str:
        card = json.loads(self.posts[n]["attachments"][0]["content"])
        return card["actions"][0]["url"]


APP_ID = "aa5e1a12-7bda-4b2d-8abb-b0d0ad1dc2d2"
APP_TAB = {"id": "t9", "configuration": {"entityId": "aidl_tab"}, "teamsApp": {"id": APP_ID}}


@override_settings(TEAMS_CHANNEL_TAB=True, TEAMS_APP_ID=APP_ID)
class HomeCardOpensTheTabTests(_Posts, TestCase):
    """Open AIDL opens the AIDL app's tab once a channel admin has added it;
    until then it opens AIDL's pages (never a dead link to a Website tab)."""

    def _patch(self, has_tab):
        for target, kwargs in (("api.teams_channel_tabs.ensure_app_tab", {"return_value": TAB}),
                               ("api.teams_channel_tabs.find_tab", {"return_value": APP_TAB if has_tab else None}),
                               ("api.teams_channel_tabs.remove_tab", {})):
            p = patch(target, **kwargs)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    def test_without_the_app_the_button_opens_aidl_pages(self):
        self._patch(False)
        teams_channel.publish_home(self.org, "tok")
        self.assertEqual(self._button(), teams_channel.app_url())
        self.ensure_app_tab.assert_called_once()  # the visible AIDL Website tab is still added
        self.assertNotIn("teams_app_tab_url", admin_setup.state(self.org))

    def test_with_the_app_tab_the_button_opens_it(self):
        self._patch(True)
        teams_channel.publish_home(self.org, "tok")
        link = self._button()
        self.assertTrue(link.startswith(f"https://teams.microsoft.com/l/entity/{APP_ID}/aidl_tab?"), link)
        self.assertIn("chan-1", link)
        self.assertIn("groupId=team-1", link)
        self.ensure_app_tab.assert_not_called()
        self.remove_tab.assert_called_once_with("tok", "team-1", "chan-1", teams_channel_tabs.APP_TAB_ENTITY)

    def test_the_link_is_remembered(self):
        self._patch(True)
        teams_channel.publish_home(self.org, "tok")
        teams_channel.publish_home(self.org, "tok")
        self.assertEqual(self.find_tab.call_count, 1)

    def test_a_card_is_posted_again_once_the_app_tab_turns_up(self):
        self.org.teams_welcome_message_id = "old-card"
        self.org.save(update_fields=["teams_welcome_message_id"])
        self._patch(False)
        teams_channel.ensure_home(self.org)
        self.assertEqual(self.posts, [])
        self.find_tab.return_value = APP_TAB
        teams_channel.ensure_home(self.org)  # checked an hour at most
        self.assertEqual(self.posts, [])
        admin_setup.update(self.org, teams_app_tab_checked="2000-01-01T00:00:00+00:00")
        teams_channel.ensure_home(self.org)
        self.org.refresh_from_db()
        teams_channel.ensure_home(self.org)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("/aidl_tab?", self._button())

    def test_a_new_channel_is_looked_at_again(self):
        self._patch(True)
        admin_setup.update(self.org, teams_app_tab_url="https://old", teams_app_tab_channel="chan-0")
        teams_channel.publish_home(self.org, "tok")
        self.assertIn("chan-1", self._button())

    @override_settings(TEAMS_CHANNEL_TAB=False)
    def test_switched_off(self):
        self._patch(True)
        teams_channel.publish_home(self.org, "tok")
        self.find_tab.assert_not_called()
        self.ensure_app_tab.assert_not_called()
        self.assertEqual(self._button(), teams_channel.app_url())

    def test_adding_the_tab_posts_the_card_again(self):
        from . import teams_portal

        self._patch(True)
        self.org.teams_welcome_message_id = "old-card"
        self.org.save(update_fields=["teams_welcome_message_id"])
        with patch("api.teams_portal.in_background", side_effect=lambda fn, *a: fn(*a)), \
                patch.object(teams_portal, "TAB_ADDED_WAITS", (0,)):
            resp = APIClient().post("/api/teams/app/tab/added/", {"team": "team-1"})
            self.assertEqual(resp.status_code, 200)
            APIClient().post("/api/teams/app/tab/added/", {"team": "team-1"})  # once is enough
            APIClient().post("/api/teams/app/tab/added/", {"team": "someone-else"})
        self.assertEqual(len(self.posts), 1)
        self.assertIn("/aidl_tab?", self._button())

    def test_the_setup_page_saves_the_tab_teams_can_open(self):
        resp = APIClient().get("/api/teams/app/tab/config/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["entity_id"], teams_channel_tabs.CUSTOM_TAB_ENTITY)
        self.assertTrue(resp.context["content_url"].endswith("/api/teams/app/tab/"))
        self.assertContains(resp, "registerOnSaveHandler")


class ManifestTests(TestCase):
    def test_the_package_has_the_channel_tab_and_matches_the_manifest(self):
        import zipfile
        from pathlib import Path

        folder = Path(settings.BASE_DIR) / "teams"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        tab = manifest["configurableTabs"][0]
        self.assertTrue(tab["configurationUrl"].endswith("/api/teams/app/tab/config/"))
        self.assertEqual(tab["scopes"], ["team"])
        self.assertNotIn("bots", manifest)
        packed = json.loads(zipfile.ZipFile(folder / "aidl-app-package.zip").read("manifest.json"))
        self.assertEqual(packed, manifest)


class EnsureAppTabTests(TestCase):
    URL = "https://aidl-backend.onrender.com/api/teams/app/tab/"

    def _tab(self, content_url, web="https://teams/old"):
        return {"id": "tab-1", "webUrl": web,
                "configuration": {"entityId": teams_channel_tabs.APP_TAB_ENTITY, "contentUrl": content_url}}

    def test_sign_in_cleanup_never_removes_it(self):
        self.assertFalse(teams_channel_tabs.APP_TAB_ENTITY.startswith("aidl-"))

    def test_an_existing_tab_is_reused(self):
        with patch.object(teams_channel_tabs, "list_channel_tabs", return_value=[self._tab(self.URL)]), \
                patch.object(teams_channel_tabs, "create_channel_website_tab") as create:
            url = teams_channel_tabs.ensure_app_tab("tok", team_id="T", channel_id="C", content_url=self.URL)
        self.assertEqual(url, "https://teams/old")
        create.assert_not_called()

    def test_a_tab_pointing_elsewhere_is_replaced(self):
        with patch.object(teams_channel_tabs, "list_channel_tabs", return_value=[self._tab("https://old/")]), \
                patch.object(teams_channel_tabs, "delete_channel_tab", return_value=True) as delete, \
                patch.object(teams_channel_tabs, "create_channel_website_tab",
                             return_value=({"webUrl": TAB}, None)) as create:
            url = teams_channel_tabs.ensure_app_tab("tok", team_id="T", channel_id="C", content_url=self.URL)
        self.assertEqual(url, TAB)
        delete.assert_called_once()
        kw = create.call_args.kwargs
        self.assertEqual((kw["entity_id"], kw["display_name"], kw["content_url"]), ("aidl_app", "AIDL", self.URL))

    def test_microsoft_refusing_gives_no_link(self):
        with patch.object(teams_channel_tabs, "list_channel_tabs", side_effect=RuntimeError("403")):
            self.assertEqual(teams_channel_tabs.ensure_app_tab("tok", team_id="T", channel_id="C",
                                                               content_url=self.URL), "")


@override_settings(DEBUG=False)
class TabPagesTests(TestCase):
    def setUp(self):
        self.org = make_org(name="Secureitlab", teams_team_id="team-1", ms_tenant_id="t1",
                            teams_welcome_channel_id="chan-1")
        self.admin = make_user(self.org, full_name="Asha Admin")
        p = patch("api.teams_channel.dispatch_if_due")
        p.start()
        self.addCleanup(p.stop)

    def test_teams_may_show_the_pages_in_a_frame(self):
        resp = APIClient().get("/api/teams/app/tab/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("X-Frame-Options", resp.headers)
        self.assertIn("frame-ancestors", resp["Content-Security-Policy"])
        self.assertIn("https://teams.microsoft.com", resp["Content-Security-Policy"])

    def test_other_pages_still_refuse_frames(self):
        resp = APIClient().get("/api/")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertNotIn("Content-Security-Policy", resp.headers)

    def test_signed_out_tab_shows_sign_in(self):
        resp = APIClient().get("/api/teams/app/tab/")
        self.assertContains(resp, "Sign in with Microsoft")
        self.assertEqual(resp.context["login_url"], "/api/teams/app/login/?next=%2Fapi%2Fteams%2Fapp%2Ftab%2Fdone%2F")

    def test_signed_in_tab_goes_to_your_page(self):
        client = APIClient()
        client.cookies[COOKIE] = _session_token(self.admin)
        resp = client.get("/api/teams/app/tab/")
        self.assertRedirects(resp, "/api/teams/app/", fetch_redirect_response=False)

    def test_signed_out_page_in_the_tab_never_loads_microsoft_in_the_frame(self):
        resp = APIClient().get("/api/teams/app/admin/", HTTP_SEC_FETCH_DEST="iframe")
        self.assertRedirects(resp, "/api/teams/app/tab/", fetch_redirect_response=False)
        resp = APIClient().get("/api/teams/app/admin/")  # a normal window still goes to Microsoft
        self.assertIn("/api/teams/app/login/", resp["Location"])

    def test_sign_in_window_hands_the_tab_a_code(self):
        client = APIClient()
        client.cookies[COOKIE] = _session_token(self.admin)
        resp = client.get("/api/teams/app/tab/done/")
        self.assertContains(resp, "notifySuccess")
        code = resp.context["code"]
        self.assertEqual(jwt.decode(code, settings.JWT_SECRET, algorithms=["HS256"])["type"], "teams_tab_handoff")

    def test_the_code_signs_the_tab_in_with_a_cookie_teams_frames_can_use(self):
        resp = APIClient().post("/api/teams/app/tab/session/", {"code": _handoff_code(self.admin)})
        self.assertEqual(resp.status_code, 200)
        cookie = resp.cookies[COOKIE]
        self.assertEqual((cookie["samesite"], bool(cookie["secure"]), bool(cookie["httponly"])), ("None", True, True))
        data = jwt.decode(cookie.value, settings.JWT_SECRET, algorithms=["HS256"])
        self.assertEqual((data["type"], data["sub"]), ("teams_app", str(self.admin.pk)))

    def test_only_a_handoff_code_works(self):
        for code in ("", "junk", _session_token(self.admin)):
            resp = APIClient().post("/api/teams/app/tab/session/", {"code": code})
            self.assertEqual(resp.status_code, 400)
            self.assertNotIn(COOKIE, resp.cookies)

    def test_a_removed_person_cannot_use_a_code(self):
        code = _handoff_code(self.admin)
        AIDLUser.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assertEqual(APIClient().post("/api/teams/app/tab/session/", {"code": code}).status_code, 400)


@override_settings(TEAMS_CHANNEL_TAB=True, TEAMS_APP_ID=APP_ID)
class CardButtonsOpenInsideTeamsTests(TestCase):
    """A card's button opens its page inside the AIDL tab once the channel has it."""

    def setUp(self):
        self.org = make_org(name="Secureitlab", teams_team_id="team-1", ms_tenant_id="t1",
                            teams_welcome_channel_id="chan-1")
        self.admin = make_user(self.org, full_name="Asha Admin")
        p = patch("api.teams_channel.dispatch_if_due")
        p.start()
        self.addCleanup(p.stop)

    def _with_tab(self):
        link = teams_channel_tabs.app_tab_deep_link(APP_ID, team_id="team-1", channel_id="chan-1", tenant_id="t1")
        admin_setup.update(self.org, teams_app_tab_url=link, teams_app_tab_channel="chan-1")
        self.org.refresh_from_db()

    def test_without_the_tab_it_is_the_browser_page(self):
        self.assertEqual(teams_channel.card_link(self.org, "me/cards/5/"),
                         teams_channel.app_url("me/cards/5/"))

    def test_with_the_tab_it_is_a_teams_link_carrying_the_page(self):
        from urllib.parse import parse_qs, urlsplit

        self._with_tab()
        link = teams_channel.card_link(self.org, "me/cards/5/")
        parts = urlsplit(link)
        self.assertEqual((parts.netloc, parts.path), ("teams.microsoft.com", f"/l/entity/{APP_ID}/aidl_tab"))
        q = parse_qs(parts.query)
        self.assertEqual(json.loads(q["context"][0]), {"channelId": "chan-1", "subEntityId": "me/cards/5/"})
        self.assertEqual((q["groupId"], q["tenantId"]), (["team-1"], ["t1"]))

    def test_a_card_button_uses_it(self):
        self._with_tab()
        with patch("api.teams_channel.post_once") as post:
            teams_channel.start_programme(self.org)
        self.assertIn("/l/entity/", post.call_args.kwargs["button"][1])

    def test_a_signed_in_tab_shows_no_sign_in_and_asks_teams_for_the_page(self):
        client = APIClient()
        client.cookies[COOKIE] = _session_token(self.admin)
        resp = client.get("/api/teams/app/tab/", HTTP_SEC_FETCH_DEST="iframe")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "var signedIn = true;")
        self.assertEqual(client.get("/api/teams/app/tab/").status_code, 302)  # a plain browser still goes home

    def test_the_page_the_card_asked_for_is_checked(self):
        client = APIClient()
        url = lambda to: client.get("/api/teams/app/tab/page/", {"to": to}).json()["url"]
        self.assertEqual(url("me/cards/5/"), "/api/teams/app/me/cards/5/")
        for bad in ("../../admin/", "/api/", "https://evil.example/", "nope/nope/", "api/auth/me/", ""):
            self.assertEqual(url(bad), "/api/teams/app/")
