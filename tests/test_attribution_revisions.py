"""收入归属、转包识别与修订/撤回链测试。"""
from __future__ import annotations

from collections import defaultdict

from benchmark.attribution import AttributionEngine
from benchmark.revisions import RevisionError
from tests._base import ServiceTestCase


class AttributionTests(ServiceTestCase):
    def test_project_identity_holds_on_base_materials(self):
        # 主承包外部收入 + 合作方转包收入 = 合同外部总额，逐项目恒等
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        mismatches = self.ctx.attribution.check_identity(attributed)
        self.assertEqual(mismatches, [])
        grouped = defaultdict(list)
        for item in attributed.values():
            grouped[item.filing.project].append(item)
        for project_id, items in grouped.items():
            total = sum(i.external_revenue_minor for i in items)
            self.assertEqual(
                total, self.ctx.attribution.projects[project_id]["total_minor"]
            )

    def test_lead_excludes_internal_subcontract(self):
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        lead = attributed["F000-lead"]
        project = self.ctx.attribution.projects["P000"]
        self.assertEqual(lead.role, "lead")
        self.assertEqual(
            lead.external_revenue_minor,
            lead.filing.effective_revenue_minor
            - project["internal_subcontract_minor"],
        )
        partner = attributed["F000-partner"]
        self.assertEqual(partner.role, "partner")
        self.assertEqual(
            partner.external_revenue_minor, partner.filing.effective_revenue_minor
        )

    def test_no_double_count_of_joint_project(self):
        # 联合项目双方外部收入相加不得超过合同外部总额，联盟内转包不重复进分母
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        grouped = defaultdict(list)
        for item in attributed.values():
            grouped[item.filing.project].append(item)
        for items in grouped.values():
            self.assertLessEqual(
                sum(i.external_revenue_minor for i in items),
                max(i.project_total_minor for i in items),
            )

    def test_revision_replace_and_withdraw_chain(self):
        # 资料内 RV1（replace→88000）与 RV2（withdraw→恢复100000）
        effective = self.ctx.revisions.effective_filings()
        filing = effective["F000-lead"]
        self.assertEqual(filing.effective_revenue_minor, 100000)
        self.assertTrue(filing.withdrawn)
        self.assertEqual([e.id for e in filing.history], ["RV1", "RV2"])

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
        self.assertEqual(
            self.ctx.revisions.effective_filings()["F000-lead"].effective_revenue_minor,
            95000,
        )

    def test_revision_version_must_increase_and_is_not_persisted(self):
        before = len(self.ctx.revisions.events)
        with self.assertRaises(RevisionError):
            self.ctx.revisions.receive_revision(
                {
                    "id": "RVX",
                    "filing": "F000-lead",
                    "version": 2,
                    "revenue_minor": 1,
                    "received_on": "2026-11-02",
                    "action": "replace",
                }
            )
        self.assertEqual(len(self.ctx.revisions.events), before)

    def test_negative_external_revenue_rejected(self):
        # 修订后收入低于转包额时归属结果必须报错，不能静默产生负外部收入
        self.ctx.revisions.receive_revision(
            {
                "id": "RVNEG",
                "filing": "F000-lead",
                "version": 4,
                "revenue_minor": 100,
                "received_on": "2026-11-03",
                "action": "replace",
            }
        )
        with self.assertRaises(ValueError):
            self.ctx.attribution.attribute(
                self.ctx.revisions.effective_filings()["F000-lead"]
            )

    def test_published_seed_total_reproduced(self):
        # 归属逻辑必须复现历史已发布准确数 27,978,600
        attributed = self.ctx.attribution.attribute_all(
            self.ctx.revisions.effective_filings()
        )
        totals = defaultdict(int)
        for item in attributed.values():
            if "2026-02" <= item.filing.period <= "2026-08":
                totals[item.filing.enterprise] += item.external_revenue_minor
        members = [f"E{i:02d}" for i in range(12)]
        self.assertEqual(sum(totals[m] for m in members), 27978600)
