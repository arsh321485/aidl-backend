"""Sharing a licence: the public page behind every Share button, the website's verify check, and that every
share link points at them."""

import json
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

from django.test import TestCase
from django.utils import timezone

from . import employee_flow, licence_image
from .models import AIDLUser
from .tests import make_org, make_user

NUMBER = "AIDL-L-6685-3154"


class LicenceShareTests(TestCase):
    def setUp(self):
        self.org = make_org(name="Secureitlab", slack_team_id="T9", slack_channel_id="C42")
        self.member = make_user(
            self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U2", full_name="Jordan Ellis",
            licence_issued=True, licence_number=NUMBER, licence_issued_at=timezone.now(),
            licence_expires_at=timezone.now() + timedelta(days=365))

    def test_shared_link_opens_the_licence_page_with_a_link_preview(self):
        resp = self.client.get(f"/api/licenses/{NUMBER}/?s={licence_image.signature(NUMBER)}")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Jordan Ellis", html)
        self.assertIn("Secureitlab", html)
        self.assertIn('property="og:image"', html)  # the preview LinkedIn / X / Facebook / WhatsApp show
        self.assertIn(f"/api/licenses/{NUMBER}.png?s=", html)
        self.assertIn("summary_large_image", html)

    def test_link_without_the_owners_signature_only_confirms_with_a_short_name(self):
        html = self.client.get(f"/api/licenses/{NUMBER}/").content.decode()
        self.assertIn("Jordan E.", html)
        self.assertNotIn("Ellis", html)
        self.assertNotIn(".png", html)  # no licence picture for someone who only has the number
        self.assertNotIn("og:image", html)
        wrong = self.client.get(f"/api/licenses/{NUMBER}/?s=nope").content.decode()
        self.assertNotIn("Ellis", wrong)

    def test_unknown_or_revoked_licence_is_a_404_page_not_a_crash(self):
        self.assertEqual(self.client.get("/api/licenses/AIDL-L-0000-0000/").status_code, 404)
        self.assertEqual(self.client.get("/api/licenses/not-a-number/").status_code, 404)
        AIDLUser.objects.filter(pk=self.member.pk).update(licence_issued=False)
        self.assertEqual(self.client.get(f"/api/licenses/{NUMBER}/?s={licence_image.signature(NUMBER)}").status_code,
                         404)

    def test_expired_and_suspended_licences_say_so(self):
        AIDLUser.objects.filter(pk=self.member.pk).update(licence_expires_at=timezone.now() - timedelta(days=1))
        html = self.client.get(f"/api/licenses/{NUMBER}/").content.decode()
        self.assertIn("This licence is expired", html)
        self.assertNotIn("current in AIDL", html)

    def test_verify_check_for_the_website(self):
        data = self.client.get("/api/licenses/verify/", {"lic": NUMBER.lower()}).json()
        self.assertTrue(data["found"])
        self.assertEqual(data["name"], "Jordan E.")
        self.assertEqual(data["org"], "Secureitlab")
        self.assertEqual(data["status"], "active")
        self.assertEqual(self.client.get("/api/licenses/verify/", {"lic": "AIDL-L-1111-2222"}).json(), {"found": False})
        self.assertEqual(self.client.get("/api/licenses/verify/").json(), {"found": False})

    def test_licence_image_route_still_works_next_to_the_page(self):
        self.assertEqual(self.client.get(f"/api/licenses/{NUMBER}.png?s=bad").status_code, 404)

    def test_every_share_button_in_slack_points_at_the_licence_page(self):
        page = licence_image.share_url(NUMBER)
        self.assertTrue(page.startswith("http") and f"/api/licenses/{NUMBER}/?s=" in page)
        view = employee_flow.share_view(self.member, self.org)
        urls = [e["url"] for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(len(urls), 3)  # X, Facebook, WhatsApp
        for url in urls:
            parsed = urlparse(url)
            shared = (parse_qs(parsed.query).get("url") or parse_qs(parsed.query).get("u")
                      or [unquote(parse_qs(parsed.query)["text"][0]).split(" ")[-1]])[0]
            self.assertEqual(shared, page, url)
        self.assertIn(page, json.dumps(view))  # the copy-link line
        card = employee_flow.licence(self.member.__class__.objects.get(pk=self.member.pk), self.org)
        self.assertNotIn("#verify", json.dumps(card))

    def test_user_serializer_gives_the_website_its_share_link(self):
        from .serializers import AIDLUserSerializer

        self.assertEqual(AIDLUserSerializer(self.member).data["licence_share_url"], licence_image.share_url(NUMBER))
        self.member.licence_issued = False
        self.assertEqual(AIDLUserSerializer(self.member).data["licence_share_url"], "")
