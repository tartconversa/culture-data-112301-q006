"""业务口径变更与可比性标注。

资料中的 definition_changes 描述某业务类别在生效月起从旧口径切换到新口径：

- 生效月及之后、且申报口径已经是新口径的申报：直接可比；
- 生效月之前的旧口径申报，若项目列在 exact_projects 中：可按项目级依据精确换算
  （本数据中换算等价于按外部净额归属，由 attribution 统一完成），标注为
  converted；
- 生效月之前的旧口径申报但缺少精确换算依据：标注 incomparable，只能排除并说明，
  不得猜测换算系数；
- 转包口径申报不受主承包口径变更影响，始终可比；
- 没有任何变更覆盖的业务类别：全部可比。

口径标注只描述“能否进入同一口径的比较”，不修改原始申报。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .revisions import EffectiveFiling

SUBCONTRACT_BASIS = "subcontract"


class Comparability(str, Enum):
    COMPARABLE = "comparable"        # 同口径，可直接进入比较
    CONVERTED = "converted"          # 旧口径但有项目级精确换算依据
    INCOMPARABLE = "incomparable"    # 缺换算依据，只能排除


@dataclass(frozen=True)
class CaliberRule:
    id: str
    service: str
    effective_period: str  # 含该月起生效
    old_basis: str
    new_basis: str
    exact_projects: frozenset[str]


@dataclass(frozen=True)
class CaliberAssessment:
    filing_id: str
    comparability: Comparability
    rule_id: str | None
    reason: str

    @property
    def usable(self) -> bool:
        return self.comparability is not Comparability.INCOMPARABLE


class CaliberRegistry:
    def __init__(self, changes: list[dict]):
        self.rules = [
            CaliberRule(
                id=row["id"],
                service=row["service"],
                effective_period=row["effective_period"],
                old_basis=row["old_basis"],
                new_basis=row["new_basis"],
                exact_projects=frozenset(row["exact_projects"]),
            )
            for row in changes
        ]
        self._by_service: dict[str, list[CaliberRule]] = {}
        for rule in self.rules:
            self._by_service.setdefault(rule.service, []).append(rule)

    def assess(self, filing: EffectiveFiling) -> CaliberAssessment:
        for rule in self._by_service.get(filing.service, ()):
            if filing.period >= rule.effective_period:
                # 转包合作方按实际服务收入记账，不承载主承包 gross→net 口径切换
                if filing.basis == SUBCONTRACT_BASIS:
                    return CaliberAssessment(
                        filing.id,
                        Comparability.COMPARABLE,
                        rule.id,
                        f"转包申报按实际服务收入记账，不受{rule.id}主承包口径切换影响",
                    )
                if filing.basis == rule.new_basis:
                    return CaliberAssessment(
                        filing.id,
                        Comparability.COMPARABLE,
                        rule.id,
                        f"生效月{rule.effective_period}之后按新口径{rule.new_basis}申报",
                    )
                # 生效后仍用旧口径：数据契约中视为待澄清，保守地不可比
                return CaliberAssessment(
                    filing.id,
                    Comparability.INCOMPARABLE,
                    rule.id,
                    f"生效月{rule.effective_period}之后仍为旧口径{rule.old_basis}，缺少换算依据",
                )
            # 生效月之前：只有旧主承包口径才受变更影响；转包等其他口径直接可比
            if filing.basis != rule.old_basis:
                continue
            if filing.project in rule.exact_projects:
                return CaliberAssessment(
                    filing.id,
                    Comparability.CONVERTED,
                    rule.id,
                    f"项目{filing.project}具备精确换算依据，可由{rule.old_basis}换算为{rule.new_basis}",
                )
            return CaliberAssessment(
                filing.id,
                Comparability.INCOMPARABLE,
                rule.id,
                f"生效月{rule.effective_period}之前的{rule.old_basis}申报且无精确换算依据",
            )
        return CaliberAssessment(
            filing.id,
            Comparability.COMPARABLE,
            None,
            "无口径变更覆盖，按原申报口径比较",
        )

    def assessments(self, filings: dict[str, EffectiveFiling]) -> dict[str, CaliberAssessment]:
        return {fid: self.assess(f) for fid, f in filings.items()}

    def comparable_filings(
        self, filings: dict[str, EffectiveFiling]
    ) -> dict[str, EffectiveFiling]:
        return {
            fid: f
            for fid, f in filings.items()
            if self.assess(f).usable
        }
