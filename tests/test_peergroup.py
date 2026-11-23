"""可比组、共同分母相容性、支配性与放宽建议测试。"""
from __future__ import annotations

from tests._base import ServiceTestCase


class PeerGroupTests(ServiceTestCase):
    def _benchmark(self, request_id="REQ1"):
        return self.service.benchmark(self.request(request_id))

    def test_req1_group_too_small_and_offers_relaxation(self):
        result = self._benchmark()
        for group in result["groups"]:
            self.assertFalse(group["sufficient"])
            self.assertGreaterEqual(len(group["relaxations"]), 1)
            self.assertIn("drop_market", [r["step"] for r in group["relaxations"]])

    def test_requester_excluded_from_peer_distribution(self):
        result = self._benchmark()
        for group in result["groups"]:
            # exclude_self=true：参考分布不得包含发起方 E00
            self.assertNotIn("E00", group["peer_members"])
            # 等权权重只覆盖参考企业且合计为 1
            self.assertAlmostEqual(sum(group["weights"].values()), 1.0)
            self.assertNotIn("E00", group["weights"])

    def test_dominance_flagged_with_explanation(self):
        result = self._benchmark()
        flagged = [g for g in result["groups"] if g["dominance"]["flagged"]]
        self.assertTrue(flagged)
        for group in flagged:
            self.assertGreater(
                group["dominance"]["top_revenue_share"],
                group["dominance"]["threshold"],
            )
            self.assertIn("支配", group["dominance"]["explanation"])

    def test_metrics_with_incompatible_populations_not_merged(self):
        result = self._benchmark()
        comp = result["compatibility"]
        self.assertFalse(comp["compatible"])
        self.assertIn("不相容", comp["note"])
        # 两指标人群确实不同（E00 缺已完成合同时不进 rev-contract）
        self.assertNotEqual(
            comp["populations"]["rev-hour"], comp["populations"]["rev-contract"]
        )

    def test_relaxation_restates_comparison_meaning(self):
        result = self._benchmark()
        steps = {r["step"]: r["meaning"] for r in result["groups"][0]["relaxations"]}
        self.assertIn("widen_services", steps)
        self.assertIn("口径", steps["widen_services"])

    def test_coverage_reported_per_period(self):
        result = self._benchmark()
        for group in result["groups"]:
            if group["statistics"].get("available"):
                self.assertTrue(group["coverage"]["available"])
                self.assertIn("period_revenue_coverage", group["coverage"])
                for ratio in group["coverage"]["period_revenue_coverage"].values():
                    self.assertLessEqual(ratio, 1.0)

    def test_market_filter_intersects_member_scope(self):
        result = self._benchmark()
        ent = self.ctx.store.by_id("enterprises")
        for group in result["groups"]:
            for member in group["members"]:
                self.assertEqual(ent[member]["market"], "EU")
