"""权限隔离、版本绑定与漂移影响测试。"""
from __future__ import annotations

from benchmark.access import AccessError
from tests._base import ServiceTestCase


class AccessTests(ServiceTestCase):
    def test_enterprise_cannot_read_other_detail(self):
        with self.assertRaises(AccessError):
            self.service.self_detail("E00", "E01")

    def test_enterprise_reads_own_detail(self):
        detail = self.service.self_detail("E00", "E00")
        self.assertEqual(detail["enterprise"], "E00")
        self.assertTrue(detail["filings"])
        for row in detail["filings"]:
            self.assertIn("external_revenue_minor", row)

    def test_member_view_has_no_identifiable_peers(self):
        result = self.service.benchmark(self.request("REQ1"))
        view = self.ctx.access.member_view(result, "E00")
        for group in view["groups"]:
            self.assertNotIn("peer_members", group)
            self.assertNotIn("members", group)
            self.assertIsInstance(group["peer_count"], int)

    def test_public_view_contains_only_allowed_dispositions(self):
        review = self.service.review_publication(self.request("REQ3"), "2026-11-23")
        for cell in review["public_view"]:
            self.assertIn(cell["disposition"], {"exact", "interval"})

    def test_audience_isolation(self):
        entries = [e for e in self.ctx.ledger.entries if e["event"] == "published"]
        self.assertTrue(entries)
        self.assertTrue(
            all(self.ctx.access.enforce_ledger_audience(e, "public") for e in entries)
        )


class VersionTests(ServiceTestCase):
    def test_outputs_bind_data_version(self):
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        self.assertIn("data_version", review)
        self.assertTrue(review["data_version"])
        benchmark = self.service.benchmark(self.request("REQ1"))
        self.assertEqual(benchmark["data_version"], review["data_version"])

    def test_revision_changes_data_version_and_pending_review(self):
        before = self.ctx.data_version
        self.ctx.revisions.receive_revision(
            {
                "id": "RV3",
                "filing": "F000-lead",
                "version": 4,
                "revenue_minor": 95000,
                "received_on": "2026-11-01",
                "action": "replace",
            }
        )
        self.assertNotEqual(before, self.ctx.data_version)
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        self.assertEqual(review["data_version"], self.ctx.data_version)

    def test_version_drift_reports_impact_scope(self):
        review = self.service.review_publication(
            {**self.request("REQ2"), "data_version": "stale-version"},
            "2026-11-23",
        )
        drift = review["version_drift"]
        self.assertTrue(drift["drift"])
        self.assertIn("affected_historical_cells", drift)
        self.assertIn("历史发布保留原依据", drift["note"])

    def test_historical_publication_keeps_original_basis_after_revision(self):
        # 发布后再修订：台账中的历史格数值与 data_version 均不变
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        entries = self.service.approve(review, "2026-11-23")
        snapshot = [(e["id"], e["lower_minor"], e["upper_minor"], e["data_version"]) for e in entries]
        self.ctx.revisions.receive_revision(
            {
                "id": "RV3",
                "filing": "F000-lead",
                "version": 4,
                "revenue_minor": 95000,
                "received_on": "2026-11-01",
                "action": "replace",
            }
        )
        later = [
            (e["id"], e["lower_minor"], e["upper_minor"], e["data_version"])
            for e in self.ctx.ledger.entries
            if e["event"] == "published" and e["id"] in {s[0] for s in snapshot}
        ]
        self.assertEqual(sorted(snapshot), sorted(later))
