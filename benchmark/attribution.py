"""收入归属、实际服务产出与转包识别。

契约口径（docs/资料契约.md）：
- external-revenue 在主承包方 = 外部合同总额 − 联盟内转包；
  在合作方 = 其实际转包服务收入。
- 同一份联合项目合同同时出现主承包 gross-prime 与合作方 subcontract 申报，
  若直接把申报行金额相加，联盟内转包会被计两次。归属后恒等：
  主承包外部收入 + 合作方转包收入 = 合同外部总额（total_minor），
  因此联合项目总额不会被重复放进任何分母。
- 项目数按成员“实际完成的服务合同”解释，不数申报行；未完成项目保留其收入与
  工时记录，但不进入 completed-contracts 分母，也不得按零收益处理。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .calibers import CaliberAssessment, Comparability
from .materials import MaterialStore
from .revisions import EffectiveFiling

GROSS_PRIME = "gross-prime"
SUBCONTRACT = "subcontract"


@dataclass(frozen=True)
class AttributedFiling:
    filing: EffectiveFiling
    external_revenue_minor: int          # 归属后外部收入
    internal_subcontract_minor: int      # 该行承载的联盟内转包额
    role: str                            # lead / partner
    project_total_minor: int
    project_completed: bool
    assessment: CaliberAssessment

    @property
    def usable(self) -> bool:
        return self.assessment.usable


class AttributionEngine:
    def __init__(self, store: MaterialStore, assessments: dict[str, CaliberAssessment]):
        self.store = store
        self.projects = store.by_id("projects")
        self.assessments = assessments

    def attribute(self, filing: EffectiveFiling) -> AttributedFiling:
        project = self.projects[filing.project]
        subcontract = int(project["internal_subcontract_minor"])
        if filing.basis == GROSS_PRIME:
            role = "lead"
            external = filing.effective_revenue_minor - subcontract
            internal = subcontract
        else:
            role = "partner"
            # 合作方申报的收入就是其实际转包服务收入
            external = filing.effective_revenue_minor
            internal = filing.effective_revenue_minor
        if external < 0:
            raise ValueError(
                f"归属后外部收入为负：{filing.id}（修订后收入低于联盟内转包额）"
            )
        return AttributedFiling(
            filing=filing,
            external_revenue_minor=external,
            internal_subcontract_minor=internal,
            role=role,
            project_total_minor=int(project["total_minor"]),
            project_completed=project["completed_on"] is not None,
            assessment=self.assessments[filing.id],
        )

    def attribute_all(
        self, filings: dict[str, EffectiveFiling]
    ) -> dict[str, AttributedFiling]:
        return {fid: self.attribute(f) for fid, f in filings.items()}

    def check_identity(self, attributed: dict[str, AttributedFiling]) -> list[dict[str, Any]]:
        """项目级归属恒等式对账。

        基础资料中主承包与合作方申报成对，恒等式必须全部成立；运行期单方修订
        可能在对手方修订到达前造成暂时偏差，此时返回对账偏差项，由编排层作为
        告警随结果输出，而不是让整个应用无法启动。
        """
        by_project: dict[str, list[AttributedFiling]] = {}
        for item in attributed.values():
            by_project.setdefault(item.filing.project, []).append(item)
        mismatches: list[dict[str, Any]] = []
        for project_id, items in sorted(by_project.items()):
            project = self.projects[project_id]
            total = sum(item.external_revenue_minor for item in items)
            expected = int(project["total_minor"])
            if total != expected:
                mismatches.append(
                    {
                        "project": project_id,
                        "attributed_total_minor": total,
                        "contract_total_minor": expected,
                        "difference_minor": total - expected,
                        "open": project["completed_on"] is None,
                        "note": "单方修订或申报未齐套，待对手方修订到达后重新对账",
                    }
                )
        return mismatches

    @staticmethod
    def usable(items: list[AttributedFiling]) -> list[AttributedFiling]:
        return [item for item in items if item.usable]

    @staticmethod
    def completed_contracts(items: list[AttributedFiling]) -> set[str]:
        """成员实际完成的服务合同集合（按项目去重，不数申报行）。"""
        return {
            item.filing.project
            for item in items
            if item.usable and item.project_completed
        }

    @staticmethod
    def open_projects(items: list[AttributedFiling]) -> set[str]:
        return {
            item.filing.project
            for item in items
            if item.usable and not item.project_completed
        }

    @staticmethod
    def explain_exclusions(items: list[AttributedFiling]) -> list[dict]:
        """逐份说明被口径标注排除的申报，供成员分析留痕。"""
        notes = []
        for item in items:
            if item.assessment.comparability is Comparability.INCOMPARABLE:
                notes.append(
                    {
                        "filing": item.filing.id,
                        "project": item.filing.project,
                        "period": item.filing.period,
                        "service": item.filing.service,
                        "rule": item.assessment.rule_id,
                        "reason": item.assessment.reason,
                    }
                )
        return notes
