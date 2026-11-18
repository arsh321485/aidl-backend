"""The Trial includes 3 policy topics (Passwords & Sign-in, Email, Chat & Social Media, Data Classification); the
other 11 are shown after them, marked as paid, and open an upgrade screen."""

import json

from django.test import TestCase

from . import aup_store, policy_cards as pc, policy_cards_slack as pcs
from .tests import make_org, make_user
from .tests_trial import trial_org


def _text(view) -> str:
    return json.dumps(view, ensure_ascii=False)


class TrialTopicTests(TestCase):
    def setUp(self):
        self.org = trial_org()
        self.paid = make_org(name="Paid", plan="basic")

    def test_names(self):
        self.assertEqual([pc.SHORT[n] for n in pc.FREE_TOPICS],
                         ["Passwords & Sign-in", "Email, Chat & Social Media", "Data Classification"])

    def test_order_free_first_then_paid(self):
        order = pc.ordered(self.org)
        self.assertEqual(order[:3], [5, 8, 6])
        self.assertEqual(sorted(order), list(pc.TOPICS))
        self.assertEqual(order[3:], [n for n in pc.TOPICS if n not in pc.FREE_TOPICS])
        self.assertEqual(pc.ordered(self.paid), list(pc.TOPICS))

    def test_locking(self):
        self.assertTrue(all(pc.is_locked(self.org, n) for n in pc.TOPICS if n not in pc.FREE_TOPICS))
        self.assertFalse(any(pc.is_locked(self.org, n) for n in pc.FREE_TOPICS))
        self.assertFalse(any(pc.is_locked(self.paid, n) for n in pc.TOPICS))

    def test_already_approved_card_stays_open(self):
        a = aup_store.answers(self.org)
        pc.approve(self.org, None, 2, a, deliver=False)
        self.assertFalse(pc.is_locked(self.org, 2))
        self.assertLess(pc.ordered(self.org).index(2), pc.ordered(self.org).index(1))

    def test_topics_list_marks_paid(self):
        text = _text(pcs.topics_view(self.org, {}))
        self.assertIn("Included in your Trial", text)
        self.assertIn("Unlock with a paid plan", text)
        self.assertEqual(text.count("aidl_pc_locked"), 11)
        self.assertEqual(text.count('"action_id": "aidl_pc_pick"'), 3)
        self.assertLess(text.index("Passwords & Sign-in"), text.index("Personal devices"))
        self.assertNotIn("aidl_pc_locked", _text(pcs.topics_view(self.paid, {})))

    def test_clicking_paid_topic_shows_upgrade_screen(self):
        view = pcs.action(self.org, {}, "aidl_pc_locked", "7", "", "https://example.test/plans?token=x")
        text = _text(view)
        self.assertIn("AI tools at work is on the paid plans", text)
        self.assertIn("https://example.test/plans?token=x", text)
        self.assertEqual(pcs.action(self.org, {}, "aidl_pc_pick", "7", "", "")["callback_id"], pcs.TOPICS)
        self.assertNotIn('"url"', _text(pcs.action(self.org, {}, "aidl_pc_pick", "7", "")))
        self.assertEqual(pcs.action(self.org, {}, "aidl_pc_pick", "5", "")["callback_id"], pcs.CARD)
        self.assertEqual(pcs.action(self.paid, {}, "aidl_pc_pick", "7", "")["callback_id"], pcs.CARD)

    def test_cannot_approve_paid_topic(self):
        view = {"private_metadata": json.dumps({"no": 7}), "state": {"values": {}}}
        owner = make_user(self.org, microsoft_id="slack:T9:U1")
        out = pcs.submit_card(owner, self.org, view)
        self.assertEqual(out["response_action"], "update")
        self.assertIsNone(aup_store.card(self.org, 7))


class RetirePaidTopicsTests(TestCase):
    def setUp(self):
        from . import employee_flow  # noqa: F401
        from .models import AIDLUser

        self.org = trial_org()
        a = aup_store.answers(self.org)
        for n in (5, 7, 9):
            pc.approve(self.org, None, n, a, deliver=False)
        self.member = make_user(self.org, role=AIDLUser.Role.LEARNER, microsoft_id="slack:T9:U5")
        from .models import Acknowledgement

        for n in (5, 7, 9):
            Acknowledgement.objects.create(organization_id=str(self.org.pk), user_id=str(self.member.pk),
                                           item=aup_store.point_item(n), version=aup_store.card_version(self.org, n))

    def test_dry_run_changes_nothing(self):
        r = pc.retire_paid_topics(self.org)
        self.assertEqual([t["no"] for t in r["topics"]], [7, 9])
        self.assertEqual(r["acknowledgements"], 2)
        self.assertEqual(sorted(aup_store.cards(self.org)), [5, 7, 9])

    def test_apply_removes_only_paid_topics(self):
        from .models import Acknowledgement

        pc.retire_paid_topics(self.org, apply=True)
        self.org.refresh_from_db()
        self.assertEqual(sorted(aup_store.cards(self.org)), [5])
        self.assertEqual(sorted(Acknowledgement.objects.values_list("item", flat=True)), ["aup_5"])
        self.assertEqual(aup_store.points_accepted(self.member, self.org), {5})
        self.assertTrue(all(pc.is_locked(self.org, n) for n in (7, 9)))

    def test_paid_plan_untouched(self):
        self.org.plan = "basic"
        self.org.save()
        self.assertEqual(pc.retire_paid_topics(self.org, apply=True)["topics"], [])
        self.assertEqual(sorted(aup_store.cards(self.org)), [5, 7, 9])
