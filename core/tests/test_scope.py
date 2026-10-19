"""
"Deal scope" decides which deals the dashboard's deal figures and the pipeline
board count:
- Rep: always their own deals (`owner=user`). A `?scope=team` request is ignored.
- Viewer: always every deal (read-only role; owns nothing). No switch.
- Admin: every deal by default ("team"); `?scope=mine` switches to own deals,
  `?scope=team` switches back. The choice is remembered in the session
  (`request.session["deal_scope"]`) so it carries from dashboard to board.
- Only admins see the switch: links with `?scope=mine` and `?scope=team`.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Deal, Stage
from core.tests.factories import make_company, make_deal, make_user, PASSWORD


class DealScopeTests(TestCase):
    def setUp(self):
        now = timezone.now()
        # Users
        self.admin = make_user("admin", role="Admin")
        self.rep = make_user("rep", role="Rep")
        self.rep2 = make_user("rep2", role="Rep")
        self.viewer = make_user("viewer", role="Viewer")
        # Company
        self.company = make_company()
        # Deals
        # - rep: LEAD 1000, PROPOSAL 2000, WON 500 (closed_at now - 10 days)
        self.deal_rep_lead = make_deal(self.company, owner=self.rep, name="rep-lead", stage=Stage.LEAD, value=1000)
        self.deal_rep_prop = make_deal(self.company, owner=self.rep, name="rep-prop", stage=Stage.PROPOSAL, value=2000)
        self.deal_rep_won = make_deal(self.company, owner=self.rep, name="rep-won", stage=Stage.WON, value=500)
        # - rep2: PROPOSAL 4000, LOST 300 with lost_reason "x" (closed_at now - 5 days)
        self.deal_rep2_prop = make_deal(self.company, owner=self.rep2, name="rep2-prop", stage=Stage.PROPOSAL, value=4000)
        self.deal_rep2_lost = make_deal(self.company, owner=self.rep2, name="rep2-lost", stage=Stage.LOST, value=300, lost_reason="x")
        # - admin: NEGOTIATION 8000
        self.deal_admin_neg = make_deal(self.company, owner=self.admin, name="admin-neg", stage=Stage.NEGOTIATION, value=8000)
        # Set closed_at
        closed_at_old = now - timezone.timedelta(days=10)
        closed_at_recent = now - timezone.timedelta(days=5)
        Deal.objects.filter(pk=self.deal_rep_won.pk).update(closed_at=closed_at_old)
        Deal.objects.filter(pk=self.deal_rep2_lost.pk).update(closed_at=closed_at_recent)

    def test_rep_dashboard_pipeline_value_and_open(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["counts"]["pipeline_value"], 3000)
        self.assertEqual(resp.context["counts"]["open_deals"], 2)

    def test_rep_scope_team_ignored(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard") + "?scope=team")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["counts"]["pipeline_value"], 3000)

    def test_rep_pipeline_by_stage_proposal(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["pipeline_by_stage"][Stage.PROPOSAL]["value"], 2000)

    def test_rep_win_rate_metrics(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["win_rate"], 100)
        self.assertEqual(resp.context["won_count"], 1)
        self.assertEqual(resp.context["closed_count"], 1)

    def test_viewer_always_all(self):
        self.client.login(username="viewer", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["counts"]["pipeline_value"], 15000)
        resp = self.client.get(reverse("dashboard") + "?scope=mine")
        self.assertEqual(resp.context["counts"]["pipeline_value"], 15000)

    def test_admin_scope_session_remembered(self):
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.context["counts"]["pipeline_value"], 15000)  # default team
        resp = self.client.get(reverse("dashboard") + "?scope=mine")
        self.assertEqual(resp.context["counts"]["pipeline_value"], 8000)
        resp = self.client.get(reverse("dashboard"))  # no param
        self.assertEqual(resp.context["counts"]["pipeline_value"], 8000)  # remembered
        resp = self.client.get(reverse("dashboard") + "?scope=team")
        self.assertEqual(resp.context["counts"]["pipeline_value"], 15000)

    def test_switch_visibility_dashboard(self):
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        content = resp.content.decode()
        self.assertIn("?scope=mine", content)
        self.assertIn("?scope=team", content)
        self.client.logout()
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        content = resp.content.decode()
        self.assertNotIn("?scope=mine", content)
        self.assertNotIn("?scope=team", content)
        self.client.logout()
        self.client.login(username="viewer", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        content = resp.content.decode()
        self.assertNotIn("?scope=mine", content)
        self.assertNotIn("?scope=team", content)

    def test_qa03_pipeline_by_stage_no_won_lost_and_no_pill(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("dashboard"))
        pbs = resp.context["pipeline_by_stage"]
        self.assertNotIn("won", pbs)
        self.assertNotIn("lost", pbs)
        content = resp.content.decode()
        self.assertNotIn("stage-won", content)

    def test_viewer_pipeline_board_6_cards_and_edit_false(self):
        self.client.login(username="viewer", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertEqual(content.count('class="deal-card"'), 6)
        self.assertIn('data-can-edit="false"', content)

    def test_rep_pipeline_board_cards_and_scope_ignored(self):
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        content = resp.content.decode()
        self.assertEqual(content.count('class="deal-card"'), 3)
        resp = self.client.get(reverse("pipeline") + "?scope=team")
        content = resp.content.decode()
        self.assertEqual(content.count('class="deal-card"'), 3)

    def test_admin_pipeline_board_scope(self):
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        content = resp.content.decode()
        self.assertEqual(content.count('class="deal-card"'), 6)
        resp = self.client.get(reverse("pipeline") + "?scope=mine")
        content = resp.content.decode()
        self.assertEqual(content.count('class="deal-card"'), 1)

    def test_admin_board_shows_switch_links(self):
        self.client.login(username="admin", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        content = resp.content.decode()
        self.assertIn("?scope=mine", content)
        self.assertIn("?scope=team", content)
        self.client.logout()
        self.client.login(username="rep", password=PASSWORD)
        resp = self.client.get(reverse("pipeline"))
        content = resp.content.decode()
        self.assertNotIn("?scope=mine", content)
        self.assertNotIn("?scope=team", content)

    def test_admin_move_with_mine_scope_shows_mine_totals(self):
        self.client.login(username="admin", password=PASSWORD)
        # Set scope to mine
        self.client.get(reverse("pipeline") + "?scope=mine")
        # Move their own NEGOTIATION deal (8000) to WON
        resp = self.client.post(
            reverse("deal_stage_move", args=[self.deal_admin_neg.pk]),
            {"stage": Stage.WON.value},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # OOB col-head-won shows "$8,000 · 1" (mine)
        self.assertIn("col-head-won", content)
        self.assertIn("$8,000 · 1", content)

    def test_admin_move_default_scope_shows_team_totals(self):
        self.client.login(username="admin", password=PASSWORD)
        # Default is team
        resp = self.client.post(
            reverse("deal_stage_move", args=[self.deal_rep_lead.pk]),
            {"stage": Stage.QUALIFIED.value},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        # col-head-lead shows $0 · 0 and col-head-qualified $1,000 · 1 (team)
        self.assertIn("col-head-lead", content)
        self.assertIn("$0 · 0", content)
        self.assertIn("col-head-qualified", content)
        self.assertIn("$1,000 · 1", content)
