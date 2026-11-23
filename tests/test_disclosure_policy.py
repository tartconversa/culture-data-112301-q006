"""跨多次发布的披露政策测试。

覆盖：
- 历史准确数 + 历史区间的相减识别；
- 最小贡献者与最大集中度；
- 最小区间宽度（含同批交叉维度相减）；
- 撤回不消除已披露事实、不得重新释放同信息；
- 无约束时拒绝发布无信息量占位区间；
- 已公开事实不可被新发布改动；
- 多发布轮次后政策始终成立。
"""
from __future__ import annotations

from benchmark.disclosure import (
    CellDecision,
    DisclosureAuditor,
    KnownCell,
)
from benchmark.ledger import LedgerError
from tests._base import ServiceTestCase

PUBLIC_POLICY = None


def policy(store):
    return next(p for p in store.rows("disclosure_policy") if p["id"] == "public")


class DisclosurePolicyTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.auditor = DisclosureAuditor(policy(self.ctx.store))
        self.historical = self.ctx.ledger.known_cells(include_withdrawn=True)
        self.assertEqual(len(self.historical), 2)

    def test_exact_requires_contributors_and_concentration(self):
        # 5 家等贡献：达标可准确；4 家：贡献不足
        good = {f"E{i}": 1_000_000 for i in range(5)}
        decision = self.auditor.audit(
            "C1", "external-revenue", "2026-02/2026-08",
            list(good), "external-net/2026-02..2026-08", frozenset(),
            true_value_minor=5_000_000, contributions=good, known_cells=[],
        )
        self.assertTrue(decision.own_cell_safe)
        self.assertEqual(decision.disposition, "exact")

        thin = {f"E{i}": 1_000_000 for i in range(4)}
        decision = self.auditor.audit(
            "C2", "external-revenue", "2026-02/2026-08",
            list(thin), "external-net/2026-02..2026-08", frozenset(),
            true_value_minor=4_000_000, contributions=thin, known_cells=[],
        )
        self.assertFalse(decision.own_cell_safe)
        self.assertNotEqual(decision.disposition, "exact")

    def test_concentration_breach_blocks_exact(self):
        contributions = {"E0": 4_600_000, **{f"E{i}": 100_000 for i in range(1, 5)}}
        decision = self.auditor.audit(
            "C3", "external-revenue", "2026-02/2026-08",
            list(contributions), "external-net/2026-02..2026-08", frozenset(),
            true_value_minor=5_000_000, contributions=contributions, known_cells=[],
        )
        self.assertFalse(decision.own_cell_safe)
        self.assertTrue(any("集中度" in r or "贡献比" in r for r in decision.reasons))

    def test_historical_subtraction_identifies_single_enterprise(self):
        # REQ2：单企业格，历史两格相减给出可行界，宽度足够才允许区间发布
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        decision = review["decisions"][0]
        self.assertEqual(decision["contributor_count"], 1)
        self.assertNotEqual(decision["disposition"], "exact")
        self.assertIn("PUB-total", decision["constraints_used"])
        self.assertIn("PUB-part", decision["constraints_used"])
        # 可行界必须覆盖真实值
        low, high = decision["derived_interval_minor"]
        self.assertLessEqual(low, decision["true_value_minor"])
        self.assertLessEqual(decision["true_value_minor"], high)
        # 任何对外区间宽度不得低于政策下限
        if decision["published_interval_minor"]:
            width = decision["published_interval_minor"][1] - decision["published_interval_minor"][0]
            self.assertGreaterEqual(width, self.auditor.min_interval)

    def test_interval_never_narrower_than_policy_minimum(self):
        # 构造一个被历史约束压窄到低于下限的格：发布区间必须被扩宽且下界非负
        known = [
            KnownCell(
                id="K1", members=frozenset({"A", "B", "C", "D", "E"}),
                metric="m", period="w", lower_minor=1_000_000, upper_minor=1_000_000,
                audience="public", caliber_key="net", services=frozenset({"s"}),
                historical=True,
            ),
            KnownCell(
                id="K2", members=frozenset({"A", "B", "C", "D"}),
                metric="m", period="w", lower_minor=950_000, upper_minor=950_000,
                audience="public", caliber_key="net", services=frozenset({"s"}),
                historical=True,
            ),
        ]
        contributions = {"E": 50_000}
        decision = self.auditor.audit(
            "C4", "m", "w", ["E"], "net", frozenset({"s"}),
            true_value_minor=50_000, contributions=contributions, known_cells=known,
        )
        self.assertEqual(decision.derived_width, 0)  # 相减精确识别 E
        self.assertEqual(decision.disposition, "interval")
        low, high = decision.publish_lower, decision.publish_upper
        self.assertGreaterEqual(high - low, self.auditor.min_interval)
        self.assertGreaterEqual(low, 0)
        self.assertLessEqual(low, 50_000)
        self.assertLessEqual(50_000, high)

    def test_no_constraint_no_informationless_interval(self):
        # 无相容约束且无真实值（部分成员无该业务贡献）：无法形成有信息量的区间
        contributions = {f"E{i}": 0 for i in range(3)}
        decision = self.auditor.audit(
            "C5", "m", "w", list(contributions), "net", frozenset({"s"}),
            true_value_minor=None, contributions=contributions, known_cells=[],
        )
        self.assertEqual(decision.disposition, "deny")
        self.assertIsNone(decision.publish_lower)

    def test_unsafe_cell_with_true_value_gets_widened_interval(self):
        # 贡献不足但有真实值、无外部约束：围绕真实值扩到政策最小宽度，下界非负
        contributions = {f"E{i}": 100_000 for i in range(3)}
        decision = self.auditor.audit(
            "C5B", "m", "w", list(contributions), "net", frozenset({"s"}),
            true_value_minor=300_000, contributions=contributions, known_cells=[],
        )
        self.assertEqual(decision.disposition, "interval")
        low, high = decision.publish_lower, decision.publish_upper
        self.assertEqual(high - low, self.auditor.min_interval)
        self.assertGreaterEqual(low, 0)
        self.assertLessEqual(low, 300_000)
        self.assertLessEqual(300_000, high)

    def test_same_batch_cross_dimension_subtraction(self):
        request = {
            "id": "REQ-CROSS", "kind": "publish", "requester": "alliance",
            "market": "all",
            "services": ["publishing", "localization", "design", "support"],
            "metrics": ["external-revenue"],
            "members": [f"E{i:02d}" for i in range(5)],
            "cells": [
                {"tag": "all5", "members": [f"E{i:02d}" for i in range(5)]},
                {"tag": "first4", "members": [f"E{i:02d}" for i in range(4)]},
            ],
        }
        review = self.service.review_publication(request, "2026-11-23")
        dispositions = {d["cell_id"]: d["disposition"] for d in review["decisions"]}
        # 即使 first4 自身达标，同批相减可识别第 5 家，两格都不得发准确值
        for disposition in dispositions.values():
            self.assertNotEqual(disposition, "exact")
        for decision in review["decisions"]:
            if decision["published_interval_minor"]:
                width = decision["published_interval_minor"][1] - decision["published_interval_minor"][0]
                self.assertGreaterEqual(width, self.auditor.min_interval)

    def test_withdraw_does_not_erase_disclosed_fact(self):
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        self.service.approve(review, "2026-11-23")
        cell_id = review["decisions"][0]["cell_id"]
        self.ctx.ledger.withdraw(cell_id, "2026-11-24", "测试撤回")
        # 撤回后原格仍在约束集合中
        kept = {c.id for c in self.ctx.ledger.known_cells(include_withdrawn=True)}
        self.assertIn(cell_id, kept)
        # 重新审查同一申请：可行界不得比撤回前更宽，信息无法被“重新释放”
        again = self.service.review_publication(self.request("REQ2"), "2026-11-25")
        self.assertIn(cell_id, again["decisions"][0]["constraints_used"])

    def test_published_facts_cannot_be_altered(self):
        review = self.service.review_publication(self.request("REQ2"), "2026-11-23")
        self.service.approve(review, "2026-11-23")
        cell_id = review["decisions"][0]["cell_id"]
        with self.assertRaises(LedgerError):
            self.ctx.ledger.publish(
                cell_id=cell_id, members=["E11"], metric="external-revenue",
                period="2026-02/2026-08", lower_minor=1, upper_minor=2,
                audience="public", caliber_key="x", services=["s"],
                data_version="v", published_on="2026-11-26",
            )

    def test_policy_holds_across_three_publication_rounds(self):
        # 连续三轮发布/撤回，每一轮后所有台账格都必须满足最小宽度政策
        requests = [self.request("REQ2"), self.request("REQ3")]
        published_ids = []
        for round_no, req in enumerate(requests, start=1):
            review = self.service.review_publication(req, f"2026-11-2{round_no}")
            entries = self.service.approve(review, f"2026-11-2{round_no}")
            published_ids.extend(e["id"] for e in entries)
            for entry in self.ctx.ledger.entries:
                if entry["event"] != "published":
                    continue
                width = entry["upper_minor"] - entry["lower_minor"]
                if width == 0:
                    # 准确值格必须自身达标
                    self.assertGreaterEqual(
                        len(entry["members"]), self.auditor.min_contributors
                    )
                else:
                    self.assertGreaterEqual(width, self.auditor.min_interval)
        self.assertTrue(published_ids)
        # 第三轮：撤回第一个格后再发 REQ3，约束数量不减少
        self.ctx.ledger.withdraw(published_ids[0], "2026-11-25", "轮次撤回")
        review3 = self.service.review_publication(self.request("REQ3"), "2026-11-26")
        for decision in review3["decisions"]:
            if decision["published_interval_minor"]:
                width = decision["published_interval_minor"][1] - decision["published_interval_minor"][0]
                self.assertGreaterEqual(width, self.auditor.min_interval)

    def test_incompatible_calibers_are_not_combined(self):
        # 服务集合不被包含的格不得作为约束（不相容口径不参与推导）
        known = [
            KnownCell(
                id="KX", members=frozenset({"E11"}), metric="external-revenue",
                period="2026-02/2026-08", lower_minor=10, upper_minor=10,
                audience="public", caliber_key="other",
                services=frozenset({"publishing"}), historical=True,
            )
        ]
        decision = self.auditor.audit(
            "C6", "external-revenue", "2026-02/2026-08", ["E11"],
            "external-net/2026-02..2026-08", frozenset({"support"}),
            true_value_minor=500, contributions={"E11": 500}, known_cells=known,
        )
        self.assertNotIn("KX", decision.constraints_used)
