"""The 7-day Trial: its clock, licence validity that follows the plan, awareness cards (2 a week, only while the
Trial lasts), upgrades and cancellations, and that "Learner" / class L are gone from the product."""

import json
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from . import admin_home, admin_setup, billing, licensing, plans, trial
from .models import AIDLUser, Organization, PackageDelivery
from .package_delivery import post_times, schedule_due, sync_package, within_trial
from .tests import _accept_aup, _approve_all, make_org, make_user


class Slack:
    """A stand-in for the Slack Web API that remembers every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, token, *, json=None, params=None):
        self.calls.append((method, json or params))
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D7"}}
        if method == "chat.scheduleMessage":
            return {"ok": True, "scheduled_message_id": f"Q{len(self.calls)}"}
        if method == "users.info":
            return {"ok": True, "user": {"tz_offset": 0, "profile": {}}}
        return {"ok": True, "ts": "1.1"}

    def methods(self, name):
        return [b for m, b in self.calls if m == name]


def trial_org(**kw):
    from .slack_client import encrypt_token

    return make_org(name="Northwind", slack_team_id="T9", slack_channel_id="C42", slack_bot_user_id="UBOT",
                    slack_bot_token=encrypt_token("xoxb-t"), plan="trial", **kw)


def licensed(org, uid="U2", *, issued_days_ago=1, expires_in_days=300, name="Jordan Ellis"):
    now = timezone.now()
    return make_user(org, role=AIDLUser.Role.LEARNER, microsoft_id=f"slack:T9:{uid}", full_name=name,
                     licence_issued=True, licence_number=f"AIDL-{uid[-1] * 4}-0001",
                     licence_issued_at=now - timedelta(days=issued_days_ago),
                     licence_expires_at=now + timedelta(days=expires_in_days))


def finished_onboarding(org, uid="U3"):
    """Someone who agreed to every card and has no licence yet."""
    member = make_user(org, role=AIDLUser.Role.LEARNER, microsoft_id=f"slack:T9:{uid}", full_name="Sam Rivera")
    _accept_aup(member, org)
    licensing.acknowledge(member, "highway_code")
    licensing.acknowledge(member, "traffic_light")
    return member


class TrialClockTests(TestCase):
    def setUp(self):
        self.org = trial_org()
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")

    def test_trial_is_seven_days_from_the_day_the_programme_starts(self):
        self.assertIsNone(trial.window(self.org))  # not started: no clock
        self.assertEqual(trial.summary(self.org)["started"], False)
        _approve_all(self.org)
        with patch("api.slack_client.slack_api", Slack()), patch("api.employee_flow.start_everyone", return_value=(0, 0)):
            admin_setup.send_item(self.org, "flow")
        started, ends = trial.window(Organization.objects.get(pk=self.org.pk))
        self.assertEqual(ends - started, timedelta(days=7))
        self.assertAlmostEqual((timezone.now() - started).total_seconds(), 0, delta=30)
        self.assertEqual(trial.summary(self.org)["days_left"], 7)

    def test_starting_again_never_restarts_the_clock(self):
        first = trial.start(self.org)
        again = trial.start(Organization.objects.get(pk=self.org.pk), when=timezone.now() + timedelta(days=3))
        self.assertEqual(first, again)

    def test_days_left_and_expiry(self):
        now = timezone.now()
        trial.start(self.org, when=now - timedelta(days=5, hours=2))
        self.assertEqual(trial.days_left(self.org), 2)
        self.assertFalse(trial.expired(self.org))
        trial.start(make_expired := trial_org(), when=now - timedelta(days=8))
        self.assertTrue(trial.expired(make_expired))
        self.assertEqual(trial.days_left(make_expired), 0)

    @override_settings(TRIAL_DAYS=3)
    def test_length_comes_from_a_setting(self):
        started, ends = trial.start(self.org)
        self.assertEqual(ends - started, timedelta(days=3))

    def test_an_organization_already_running_before_the_trial_existed_gets_one_fresh_trial(self):
        admin_setup.update(self.org, flow_started="2026-01-01T10:00:00")
        self.assertIsNone(json.loads(Organization.objects.get(pk=self.org.pk).billing or "{}").get("trial"))
        started, ends = trial.window(Organization.objects.get(pk=self.org.pk))
        self.assertAlmostEqual((timezone.now() - started).total_seconds(), 0, delta=30)
        self.assertEqual(ends - started, timedelta(days=7))
        # and it is remembered, not restarted on the next look
        self.assertEqual(trial.window(Organization.objects.get(pk=self.org.pk)), (started, ends))


@override_settings(SLACK_REPLY_SYNC=True)  # the Admin Center redraw runs inline, not in a thread that outlives the test
class LicenceValidityTests(TestCase):
    def setUp(self):
        self.org = trial_org()
        make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")

    def test_trial_licence_runs_to_the_end_of_the_trial(self):
        now = timezone.now()
        started, ends = trial.start(self.org, when=now - timedelta(days=2))
        member = finished_onboarding(self.org)
        self.assertEqual(licensing.issue_licence_if_ready(member, self.org), "issued")
        member.refresh_from_db()
        self.assertAlmostEqual(member.licence_expires_at.timestamp(), ends.timestamp(), delta=1)  # the database keeps ms
        self.assertLess((member.licence_expires_at - member.licence_issued_at).days, 6)

    def test_paid_plan_licence_is_valid_for_a_year(self):
        self.org.plan = "enterprise"
        self.org.save()
        member = finished_onboarding(self.org)
        self.assertEqual(licensing.issue_licence_if_ready(member, self.org), "issued")
        member.refresh_from_db()
        self.assertEqual((member.licence_expires_at - member.licence_issued_at).days, 365)

    def test_no_licence_is_issued_once_the_trial_is_over(self):
        trial.start(self.org, when=timezone.now() - timedelta(days=9))
        member = finished_onboarding(self.org)
        self.assertEqual(licensing.issue_licence_if_ready(member, self.org), "trial_ended")
        member.refresh_from_db()
        self.assertFalse(member.licence_issued)

    def test_individuals_without_an_organization_keep_a_year(self):
        person = make_user(self.org, role=AIDLUser.Role.LEARNER, organization_id="", enroll_as="individual")
        licensing.issue_individual_licence(person)
        person.refresh_from_db()
        self.assertEqual((person.licence_expires_at - person.licence_issued_at).days, 365)
        self.assertRegex(person.licence_number, r"^AIDL-\d{4}-\d{4}$")

    def test_licences_issued_before_the_change_are_not_touched(self):
        member = licensed(self.org, expires_in_days=300)
        before = member.licence_expires_at
        admin_setup.update(self.org, flow_started="2026-01-01T10:00:00")  # a Trial that was already running
        trial.window(Organization.objects.get(pk=self.org.pk))
        trial.close_finished_trials()
        member.refresh_from_db()
        self.assertAlmostEqual(member.licence_expires_at.timestamp(), before.timestamp(), delta=1)
        self.assertEqual(licensing.licence_status(member), "ACTIVE")


@override_settings(SLACK_REPLY_SYNC=True)  # the Admin Center redraw runs inline, not in a thread that outlives the test
class CardDeliveryTests(TestCase):
    def setUp(self):
        self.org = trial_org()
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        _approve_all(self.org)

    def test_trial_sends_two_cards_a_week_on_monday_and_thursday(self):
        self.assertEqual(plans.cadence(self.org), "2 cards a week (Mon, Thu, 10:00)")
        self.assertEqual(plans.schedule_of(self.org)["days"], (0, 3))
        self.assertNotIn("monthly", plans.schedule_of(self.org))
        slots = post_times(timezone.now(), 6, plans.schedule_of(self.org))
        self.assertTrue(all(t.weekday() in (0, 3) and t.hour == 10 for t in slots))

    def test_the_trial_package_is_what_its_days_can_carry(self):
        self.assertEqual(plans.total_cards("trial"), 3)  # one with the licence, then two a week for a week
        self.assertEqual(len(plans.package_cards(self.org)), 3)
        self.assertEqual(plans.usage(self.org)["cards"], 3)
        self.assertEqual(plans.total_cards("basic"), 104)
        self.assertEqual(plans.total_cards("enterprise"), 208)

    def test_slots_after_the_trial_are_dropped(self):
        now = timezone.now()
        trial.start(self.org, when=now)
        slots = post_times(now, 8, plans.schedule_of(self.org))
        kept = within_trial(self.org, slots)
        self.assertTrue(kept and all(t <= now + timedelta(days=7) for t in kept))
        self.assertLess(len(kept), len(slots))
        self.org.plan = "basic"
        self.assertEqual(within_trial(self.org, slots), slots)  # paid plans are not cut

    def test_slack_schedules_only_inside_the_trial_and_sends_nothing_after_it(self):
        now = timezone.now()
        _s, ends = trial.start(self.org, when=now)
        member = licensed(self.org, issued_days_ago=0, expires_in_days=7)
        slack = Slack()
        with patch("api.slack_client.slack_api", slack):
            result = sync_package(self.org, member)
        self.assertEqual(result["sent_now"], 1)
        rows = PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED)
        self.assertGreaterEqual(rows.count(), 1)
        self.assertTrue(all(r.post_at <= ends for r in rows))
        self.assertEqual(len(slack.methods("chat.scheduleMessage")), rows.count())
        # The Trial ends: whatever is still scheduled is cancelled, nothing new is sent or scheduled.
        trial.start(make := trial_org(), when=now - timedelta(days=9))
        with patch("api.slack_client.slack_api", slack):
            expired = sync_package(make, licensed(make, "U8", issued_days_ago=1))
        self.assertEqual(expired, {"skipped": "trial_ended"})

    def test_cards_left_over_from_the_monthly_schedule_are_cancelled(self):
        now = timezone.now()
        _s, ends = trial.start(self.org, when=now)
        member = licensed(self.org, expires_in_days=7)
        PackageDelivery.objects.create(organization_id=str(self.org.pk), user_id=str(member.pk), package="trial",
                                       card_no="006", post_order=2, post_at=now + timedelta(days=3),
                                       scheduled_message_id="Q1", slack_channel_id="D7")
        late = PackageDelivery.objects.create(organization_id=str(self.org.pk), user_id=str(member.pk),
                                              package="trial", card_no="001", post_order=3,
                                              post_at=now + timedelta(days=40), scheduled_message_id="Q2",
                                              slack_channel_id="D7")
        slack = Slack()
        with patch("api.slack_client.slack_api", slack):
            schedule_due(self.org)
        late.refresh_from_db()
        self.assertEqual(late.status, PackageDelivery.Status.CANCELLED)
        self.assertEqual(PackageDelivery.objects.filter(status=PackageDelivery.Status.SCHEDULED).count(), 1)
        self.assertEqual(slack.methods("chat.deleteScheduledMessage")[0]["scheduled_message_id"], "Q2")

    def test_teams_follows_the_same_rule(self):
        from . import teams_channel

        org = make_org(name="Contoso", plan="trial", teams_team_id="TT1", slack_team_id="")
        make_user(org, microsoft_id="ms-admin")
        _approve_all(org)
        now = timezone.now()
        _s, ends = trial.start(org, when=now)
        member = make_user(org, role=AIDLUser.Role.LEARNER, microsoft_id="ms-1", licence_issued=True,
                           licence_number="AIDL-1111-0001", licence_issued_at=now,
                           licence_expires_at=ends)
        with patch.object(teams_channel, "_post_awareness", return_value=True):
            teams_channel.sync_package(org, member)
        rows = PackageDelivery.objects.filter(user_id=str(member.pk), status=PackageDelivery.Status.SCHEDULED)
        self.assertTrue(all(r.post_at <= ends for r in rows))
        trial.start(over := make_org(name="Fabrikam", plan="trial", teams_team_id="TT2", slack_team_id=""),
                    when=now - timedelta(days=9))
        other = make_user(over, role=AIDLUser.Role.LEARNER, microsoft_id="ms-2", licence_issued=True,
                          licence_number="AIDL-2222-0001", licence_issued_at=now - timedelta(days=9),
                          licence_expires_at=now - timedelta(days=2))
        self.assertEqual(teams_channel.sync_package(over, other), {"skipped": "trial_ended"})


@override_settings(SLACK_REPLY_SYNC=True)  # the Admin Center redraw runs inline, not in a thread that outlives the test
class EndOfTrialTests(TestCase):
    def setUp(self):
        self.org = trial_org()
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        self.member = licensed(self.org, expires_in_days=7)
        now = timezone.now()
        trial.start(self.org, when=now - timedelta(days=9))  # over
        self.row = PackageDelivery.objects.create(
            organization_id=str(self.org.pk), user_id=str(self.member.pk), package="trial", card_no="006",
            post_order=2, post_at=now + timedelta(days=2), scheduled_message_id="Q1", slack_channel_id="D7")

    def test_closing_the_trial_cancels_cards_and_tells_the_admin_once(self):
        slack = Slack()
        with patch("api.slack_client.slack_api", slack):
            self.assertEqual(trial.close_finished_trials(), 1)
            self.assertEqual(trial.close_finished_trials(), 0)  # once
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, PackageDelivery.Status.CANCELLED)
        self.assertEqual(len(slack.methods("chat.deleteScheduledMessage")), 1)
        dm = [b for b in slack.methods("chat.postMessage") if b.get("channel") == "U1"]
        self.assertEqual(len(dm), 1)
        self.assertIn("Trial has ended", json.dumps(dm[0]))
        self.assertIn("extends their licences", json.dumps(dm[0]))

    def test_a_running_trial_is_left_alone(self):
        running = trial_org()
        trial.start(running, when=timezone.now() - timedelta(days=2))
        with patch("api.slack_client.slack_api", Slack()):
            self.assertFalse(trial.close_trial(running))

    def test_the_employee_is_told_the_trial_ended_instead_of_waiting_for_a_seat(self):
        from . import employee_flow

        waiting = finished_onboarding(self.org)
        card = json.dumps(employee_flow.licence(waiting, self.org))
        self.assertIn("trial has ended", card.lower())
        self.assertNotIn("free seat", card)

    def test_the_admin_center_says_so_and_offers_the_upgrade(self):
        from .slack_cards import build_admin_cards_context

        d = build_admin_cards_context(self.admin)
        blocks = admin_home.admin_home_blocks(d, self.admin)
        text = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("Trial · ended", text)
        self.assertIn("Your 7-day Trial has ended", text)
        self.assertIn("aidl_upgrade", text)


@override_settings(SLACK_REPLY_SYNC=True)  # the Admin Center redraw runs inline, not in a thread that outlives the test
class AdminCenterTrialTests(TestCase):
    def test_days_left_are_shown_and_a_warning_comes_two_days_before_the_end(self):
        from .slack_cards import build_admin_cards_context

        org = trial_org()
        admin = make_user(org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        trial.start(org, when=timezone.now() - timedelta(days=2, hours=1))
        text = json.dumps(admin_home.admin_home_blocks(build_admin_cards_context(admin), admin), ensure_ascii=False)
        self.assertIn("Trial · 5 days left", text)
        self.assertNotIn("Your Trial ends in", text)
        Organization.objects.filter(pk=org.pk).update(billing=json.dumps({"trial": {
            "started_at": (timezone.now() - timedelta(days=5, hours=1)).isoformat(),
            "ends_at": (timezone.now() + timedelta(days=1, hours=23)).isoformat()}}))
        text = json.dumps(admin_home.admin_home_blocks(
            build_admin_cards_context(AIDLUser.objects.get(pk=admin.pk)), admin), ensure_ascii=False)
        self.assertIn("Trial · 2 days left", text)
        self.assertIn("Your Trial ends in 2 days", text)

    def test_before_the_programme_starts_the_page_says_when_the_clock_starts(self):
        from .slack_cards import build_admin_cards_context

        org = trial_org()
        admin = make_user(org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        text = json.dumps(admin_home.admin_home_blocks(build_admin_cards_context(admin), admin), ensure_ascii=False)
        self.assertIn("Trial · 7 days", text)


@override_settings(SLACK_REPLY_SYNC=True)  # the Admin Center redraw runs inline, not in a thread that outlives the test
class UpgradeTests(TestCase):
    def setUp(self):
        self.org = trial_org(seats_purchased=50)
        self.admin = make_user(self.org, microsoft_id="slack:T9:U1", full_name="Priya Shah")
        _approve_all(self.org)
        self.now = timezone.now()
        trial.start(self.org, when=self.now - timedelta(days=3))
        self.active = licensed(self.org, "U2", issued_days_ago=2, expires_in_days=4)
        self.expired = licensed(self.org, "U4", issued_days_ago=10, expires_in_days=-3, name="Ria Patel")
        self.left = licensed(self.org, "U5", issued_days_ago=2, expires_in_days=4, name="Lee Chan")
        AIDLUser.objects.filter(pk=self.left.pk).update(slack_left_at=self.now)

    def upgrade(self, plan="enterprise"):
        with patch("api.slack_client.slack_api", Slack()):
            billing.activate(self.org, plan, [], {"days": [0, 1, 3, 4], "hour": 10} if plan == "enterprise" else None,
                             None, self.admin, how="test_mode")

    def test_upgrading_extends_every_licence_a_year_and_keeps_people_and_seats(self):
        before = plans.usage(self.org)
        self.upgrade("enterprise")
        until = timezone.now() + timedelta(days=365)
        for m in (self.active, self.expired, self.left):
            m.refresh_from_db()
            self.assertAlmostEqual((m.licence_expires_at - until).total_seconds(), 0, delta=60, msg=m.full_name)
            self.assertTrue(m.licence_issued)
        self.assertEqual(licensing.licence_status(self.expired), "ACTIVE")  # the one that ran out is valid again
        self.assertEqual(licensing.licence_status(self.left), "SUSPENDED")  # a removed person stays suspended
        after = plans.usage(Organization.objects.get(pk=self.org.pk))
        self.assertEqual(after["used"], before["used"])
        self.assertEqual(AIDLUser.objects.filter(organization_id=str(self.org.pk)).count(), 4)  # nobody removed
        self.assertEqual(self.org.seats_purchased, 50)

    def test_basic_extends_too_and_the_trial_is_used_up(self):
        self.upgrade("basic")
        self.active.refresh_from_db()
        self.assertGreater(self.active.licence_expires_at, timezone.now() + timedelta(days=360))
        data = json.loads(Organization.objects.get(pk=self.org.pk).billing)
        self.assertEqual(data["trial"].get("ended_by"), "upgrade")

    def test_a_licence_that_is_already_longer_is_never_shortened(self):
        long_one = licensed(self.org, "U6", expires_in_days=900, name="Long Licence")
        before = long_one.licence_expires_at
        self.upgrade("enterprise")
        long_one.refresh_from_db()
        self.assertAlmostEqual(long_one.licence_expires_at.timestamp(), before.timestamp(), delta=1)

    def test_people_who_finished_while_the_trial_was_over_get_their_licence_on_upgrade(self):
        Organization.objects.filter(pk=self.org.pk).update(billing=json.dumps({"trial": {
            "started_at": (self.now - timedelta(days=9)).isoformat(),
            "ends_at": (self.now - timedelta(days=2)).isoformat(), "closed_at": self.now.isoformat()}}))
        self.org.refresh_from_db()
        waiting = finished_onboarding(self.org, "U7")
        self.assertEqual(licensing.issue_licence_if_ready(waiting, self.org), "trial_ended")
        slack = Slack()
        with patch("api.slack_client.slack_api", slack):
            billing.activate(self.org, "enterprise", [], {"days": [0, 1, 3, 4], "hour": 10}, None, self.admin,
                             how="test_mode")
        waiting.refresh_from_db()
        self.assertTrue(waiting.licence_issued)
        self.assertGreater(waiting.licence_expires_at, timezone.now() + timedelta(days=360))

    def test_cancelling_a_paid_plan_keeps_licences_and_does_not_restart_the_trial(self):
        self.upgrade("enterprise")
        self.active.refresh_from_db()
        kept = self.active.licence_expires_at
        with patch("api.slack_client.slack_api", Slack()):
            billing.downgrade_to_trial(Organization.objects.get(pk=self.org.pk), how="stripe_cancelled")
        org = Organization.objects.get(pk=self.org.pk)
        self.assertEqual(org.plan, "trial")
        self.assertTrue(trial.expired(org))  # not a new Trial
        self.assertEqual(licensing.licence_status(AIDLUser.objects.get(pk=self.active.pk)), "ACTIVE")
        self.active.refresh_from_db()
        self.assertAlmostEqual(self.active.licence_expires_at.timestamp(), kept.timestamp(), delta=1)  # not shortened
        newcomer = finished_onboarding(org, "U9")
        self.assertEqual(licensing.issue_licence_if_ready(newcomer, org), "trial_ended")
        # a cancellation is not announced as "your Trial has ended"
        slack = Slack()
        with patch("api.slack_client.slack_api", slack):
            self.assertEqual(trial.close_finished_trials(), 0)
        self.assertFalse([b for b in slack.methods("chat.postMessage") if "Trial has ended" in json.dumps(b)])

    def test_test_workspaces_can_start_the_trial_over(self):
        with self.settings(INTERNAL_SLACK_TEAMS={"T9"}):
            self.upgrade("enterprise")
            billing.set_internal_plan(Organization.objects.get(pk=self.org.pk), "trial", "qa")
        org = Organization.objects.get(pk=self.org.pk)
        self.assertEqual(org.plan, "trial")
        self.assertIsNone(json.loads(org.billing).get("trial"))  # starts again with the next programme start


class TerminologyTests(TestCase):
    """There is no Learner's Licence and no class L any more."""

    ROOT = Path(__file__).resolve().parent.parent
    FORBIDDEN = [r"Learner['’]s Permit", r"Learner \(L\)", r"\bClass L\b", r"\bCLASS L\b", r"Level L\b",
                 r"L · Learner", r"learners-permit", r"Learner licen[cs]e", r"learner licen[cs]e",
                 r"license_class", r"licence_class", r"AIDL-L-\{"]
    # Files that may mention the old names: the tests themselves and the migration history.
    SKIP = ("tests", "migrations", "venv", "node_modules", "staticfiles", "README", "AIDL_Version1")

    def sources(self):
        for folder in ("api", "integrations", "google_chat"):
            for p in (self.ROOT / folder).rglob("*"):
                if p.suffix in (".py", ".html", ".md", ".json", ".txt") and p.is_file() \
                        and not any(s in p.parts or p.name.startswith(s) or p.stem.startswith(s) for s in self.SKIP):
                    yield p

    def test_no_source_file_uses_the_old_licence_class_wording(self):
        offenders = []
        for p in self.sources():
            text = p.read_text(encoding="utf-8", errors="ignore")
            for pattern in self.FORBIDDEN:
                if re.search(pattern, text):
                    offenders.append(f"{p.relative_to(self.ROOT)}: {pattern}")
        self.assertEqual(offenders, [])

    def test_the_user_api_has_no_licence_class(self):
        from .serializers import AIDLUserSerializer

        org = trial_org()
        member = licensed(org)
        data = AIDLUserSerializer(member).data
        self.assertNotIn("license_class", data)
        self.assertNotIn("Learner", json.dumps(data))
        self.assertFalse(hasattr(AIDLUser, "LicenseClass"))

    def test_signup_ignores_an_old_client_that_still_sends_a_class(self):
        from .serializers import SignupSerializer

        s = SignupSerializer(data={"first_name": "Ana", "last_name": "Diaz", "email": "ana@example.com",
                                   "country": "India", "state": "Delhi", "city": "Delhi", "license_class": "class_l"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("license_class", s.validated_data)

    def test_new_licence_numbers_have_no_class_letter_and_old_ones_still_verify(self):
        self.assertRegex(licensing.new_licence_number(), r"^AIDL-\d{4}-\d{4}$")
        org = trial_org()
        old = licensed(org, "U2")
        AIDLUser.objects.filter(pk=old.pk).update(licence_number="AIDL-L-6685-3154")
        data = self.client.get("/api/licenses/verify/", {"lic": "AIDL-L-6685-3154"}).json()
        self.assertTrue(data["found"])
        new = licensed(org, "U3", name="Sam Rivera")
        AIDLUser.objects.filter(pk=new.pk).update(licence_number="AIDL-4417-0932")
        self.assertTrue(self.client.get("/api/licenses/verify/", {"lic": "AIDL-4417-0932"}).json()["found"])

    def test_the_slack_cards_say_ai_driving_licence(self):
        from . import slack_blocks

        org = trial_org()
        member = licensed(org)
        member.avatar_url = ""
        text = json.dumps(slack_blocks.user_license_blocks(member, org) + slack_blocks.licence_progress_blocks(
            make_user(org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U8"), org), ensure_ascii=False)
        self.assertIn("AI Driving Licence", text)
        self.assertNotIn("Learner", text)
        self.assertNotIn("Permit", text)

    def test_the_licence_page_has_no_class(self):
        from . import licence_image

        org = trial_org()
        member = licensed(org)
        html = self.client.get(f"/api/licenses/{member.licence_number}/?s={licence_image.signature(member.licence_number)}"
                               ).content.decode()
        self.assertNotIn("Learner", html)
        self.assertNotIn("Class", html)
