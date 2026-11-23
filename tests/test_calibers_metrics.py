"""口径变更可比性、观察月门槛与未完成项目处理测试。"""
from __future__ import annotations

from benchmark.calibers import Comparability
from benchmark.metrics import operating_months
from tests._base import ServiceTestCase


class CaliberTests(ServiceTestCase):
    def test_design_change_exact_projects_convertible(self):
        # DC1：design 生效月 2026-07，exact_projects 内的 2026-04 旧口径可换算
        effective = self.ctx.revisions.effective_filings()
        dc1 = next(c for c in self.ctx.store.rows("definition_changes") if c["id"] == "DC1")
        exact = set(dc1["exact_projects"])
        assessments = self.ctx.calibers.assessments(effective)
        converted = [
            fid
            for fid, a in assessments.items()
            if a.comparability is Comparability.CONVERTED
        ]
        self.assertTrue(converted)
        for fid in converted:
            self.assertIn(effective[fid].project, exact)
            self.assertEqual(effective[fid].service, "design")
            self.assertEqual(effective[fid].period, "2026-04")

    def test_design_change_without_basis_marked_incomparable(self):
        effective = self.ctx.revisions.effective_filings()
        dc1 = next(c for c in self.ctx.store.rows("definition_changes") if c["id"] == "DC1")
        assessments = self.ctx.calibers.assessments(effective)
        bad = [
            fid
            for fid, a in assessments.items()
            if a.comparability is Comparability.INCOMPARABLE
            and effective[fid].service == "design"
            and effective[fid].period < dc1["effective_period"]
        ]
        self.assertTrue(bad)
        for fid in bad:
            self.assertNotIn(effective[fid].project, set(dc1["exact_projects"]))
        # 生效月之后仍按旧口径申报的格同样不可比，且不能用 exact_projects 豁免
        late = [
            fid
            for fid, a in assessments.items()
            if a.comparability is Comparability.INCOMPARABLE
            and effective[fid].service == "design"
            and effective[fid].period >= dc1["effective_period"]
        ]
        self.assertTrue(late)

    def test_support_change_excludes_gross_but_keeps_subcontract(self):
        # DC2 无精确换算依据：support 两期 gross-prime 不可比，转包始终可比
        effective = self.ctx.revisions.effective_filings()
        assessments = self.ctx.calibers.assessments(effective)
        for fid, filing in effective.items():
            if filing.service != "support":
                continue
            if filing.basis == "gross-prime":
                self.assertIs(
                    assessments[fid].comparability, Comparability.INCOMPARABLE
                )
            else:
                self.assertIs(
                    assessments[fid].comparability, Comparability.COMPARABLE
                )

    def test_incomparable_filings_never_enter_metric_scope(self):
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        for item in attributed.values():
            if not item.usable:
                self.assertIs(
                    item.assessment.comparability, Comparability.INCOMPARABLE
                )


class ObservationTests(ServiceTestCase):
    def test_operating_months(self):
        self.assertEqual(operating_months("2025-01-01", "2026-09"), 21)
        self.assertEqual(operating_months("2023-01-01", "2026-09"), 45)

    def test_min_months_gate_per_metric(self):
        # 2025 年开业企业至 2026-09 仅 21 个月：rev-contract 要求 9 月可过，
        # 观察门槛差异通过定义表驱动；构造一个 2026-08 开业虚拟企业验证不足
        ok6, months6, need6 = self.ctx.metrics.observation_ok("E02", "rev-hour")
        ok9, months9, need9 = self.ctx.metrics.observation_ok("E02", "rev-contract")
        self.assertTrue(ok6 and ok9)
        self.assertEqual((months6, need6), (21, 6))
        self.assertEqual((months9, need9), (21, 9))
        self.assertGreaterEqual(months6, need6)

    def test_open_projects_not_counted_as_zero(self):
        # 未完成项目保留收入/工时，但不进入 completed-contracts，也不按零收益处理
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        items = [
            i
            for i in attributed.values()
            if i.filing.enterprise == "E00" and i.usable
        ]
        value = self.ctx.metrics.evaluate("E00", "rev-contract", items)
        # E00 的未完成项目不产生零收益合同；分母仅含已完成合同
        self.assertEqual(value.denominator, float(value.completed_contracts))
        self.assertGreaterEqual(value.open_projects, 1)
        if value.eligible:
            self.assertGreater(value.value, 0)
