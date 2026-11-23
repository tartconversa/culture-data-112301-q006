"""指标口径、观察要求与相容组合。

三个公开指标（data/definitions.jsonl）：
- rev-hour：external-revenue / hours，最少观察 6 个月；
- rev-contract：external-revenue / completed-contracts，最少观察 9 个月；
- concentration：top-contract-revenue / external-revenue，最少观察 6 个月。

关键约束：
1. 只有已经营月份达到指标 min_months 的企业才能参与该指标；观察月按企业开业日
   至资料最新申报月的完整自然月计算，与是否每个月都有申报无关。
2. 尚未完整履约的项目保留收入与工时，不按零收益处理；仅在 rev-contract 的
   completed-contracts 分母中排除。
3. 多指标组合必须使用共同可比企业与共同期间（共同分母原则），收入/工时与
   每合同收入不能从互不相容的人群拼成同一组结论；不相容时分别给出结果并明确
   标注，不产出合并结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .attribution import AttributedFiling


def month_index(period: str) -> int:
    year, month = period.split("-")
    return int(year) * 12 + int(month)


def months_between(start: str, end_inclusive: str) -> int:
    """两个 YYYY-MM 月份之间的月数（含两端）。"""
    return month_index(end_inclusive) - month_index(start) + 1


def operating_months(opened_on: str, as_of_period: str) -> int:
    """开业日所在月至 as_of_period（含）的完整自然月数。"""
    opened = date.fromisoformat(opened_on)
    opened_period = f"{opened.year:04d}-{opened.month:02d}"
    return months_between(opened_period, as_of_period)


@dataclass(frozen=True)
class MetricDefinition:
    id: str
    numerator: str
    denominator: str
    min_months: int
    exclude_self: bool
    minimum_group: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MetricDefinition":
        return cls(
            id=row["id"],
            numerator=row["numerator"],
            denominator=row["denominator"],
            min_months=row["min_months"],
            exclude_self=row["exclude_self"],
            minimum_group=row["minimum_group"],
        )


@dataclass
class MetricValue:
    enterprise: str
    metric: str
    value: float | None
    numerator_minor: int
    denominator: float
    periods: tuple[str, ...]
    project_count: int
    completed_contracts: int
    open_projects: int
    eligible: bool
    eligibility_note: str
    per_period_coverage: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enterprise": self.enterprise,
            "metric": self.metric,
            "value": None if self.value is None else round(self.value, 6),
            "numerator_minor": self.numerator_minor,
            "denominator": round(self.denominator, 4),
            "periods": list(self.periods),
            "project_count": self.project_count,
            "completed_contracts": self.completed_contracts,
            "open_projects": self.open_projects,
            "eligible": self.eligible,
            "eligibility_note": self.eligibility_note,
            "per_period_coverage": self.per_period_coverage,
        }


class MetricEngine:
    def __init__(
        self,
        definitions: list[dict[str, Any]],
        enterprises: dict[str, dict[str, Any]],
        as_of_period: str,
    ):
        self.definitions = {
            row["id"]: MetricDefinition.from_row(row) for row in definitions
        }
        self.enterprises = enterprises
        self.as_of_period = as_of_period

    def observation_ok(self, enterprise_id: str, metric_id: str) -> tuple[bool, int, int]:
        definition = self.definitions[metric_id]
        months = operating_months(
            self.enterprises[enterprise_id]["opened_on"], self.as_of_period
        )
        return months >= definition.min_months, months, definition.min_months

    def evaluate(
        self, enterprise_id: str, metric_id: str, items: list[AttributedFiling]
    ) -> MetricValue:
        """在给定可比申报集合上计算单个企业的单指标。"""
        definition = self.definitions[metric_id]
        usable = [item for item in items if item.usable]
        periods = tuple(sorted({item.filing.period for item in usable}))
        projects = {item.filing.project for item in usable}
        completed = {
            item.filing.project for item in usable if item.project_completed
        }
        open_projects = projects - completed
        revenue = sum(item.external_revenue_minor for item in usable)
        hours = sum(item.filing.hours for item in usable)
        top_contract = 0
        if projects:
            by_project: dict[str, int] = {}
            for item in usable:
                by_project[item.filing.project] = (
                    by_project.get(item.filing.project, 0)
                    + item.external_revenue_minor
                )
            top_contract = max(by_project.values())

        ok, months, required = self.observation_ok(enterprise_id, metric_id)
        note = f"已经营{months}个月，指标要求{required}个月"
        eligible = ok
        value: float | None = None
        denominator = 0.0

        if not eligible:
            note += "；观察月不足，不参与该指标"
        elif metric_id == "rev-hour":
            denominator = hours
            if hours > 0:
                value = revenue / hours
            else:
                eligible = False
                note += "；可比工时为零，无法形成收入/工时"
        elif metric_id == "rev-contract":
            denominator = float(len(completed))
            if completed:
                # 分子限定为已完成合同对应的外部收入；未完成项目不计零，整体排除在该指标外
                completed_revenue = sum(
                    item.external_revenue_minor
                    for item in usable
                    if item.project_completed
                )
                value = completed_revenue / len(completed)
                revenue = completed_revenue
            else:
                eligible = False
                note += "；窗口内无已完成合同，不能按零收益处理"
        elif metric_id == "concentration":
            denominator = float(revenue)
            if revenue > 0:
                value = top_contract / revenue
            else:
                eligible = False
                note += "；外部收入为零，无法计算集中度"
        else:  # 非比率总量指标（如 external-revenue），无观察门槛以外的分母
            denominator = float(revenue)
            value = float(revenue)

        coverage = self._coverage(items, periods)
        return MetricValue(
            enterprise=enterprise_id,
            metric=metric_id,
            value=value,
            numerator_minor=revenue,
            denominator=denominator,
            periods=periods,
            project_count=len(projects),
            completed_contracts=len(completed),
            open_projects=len(open_projects),
            eligible=eligible,
            eligibility_note=note,
            per_period_coverage=coverage,
        )

    def _coverage(
        self, all_items: list[AttributedFiling], periods: tuple[str, ...]
    ) -> dict[str, dict[str, float]]:
        """每个统计期内可比申报占全部申报的收入与工时覆盖比例。"""
        coverage: dict[str, dict[str, float]] = {}
        for period in periods:
            in_period = [i for i in all_items if i.filing.period == period]
            usable = [i for i in in_period if i.usable]
            rev_all = sum(i.external_revenue_minor for i in in_period)
            rev_ok = sum(i.external_revenue_minor for i in usable)
            hours_all = sum(i.filing.hours for i in in_period)
            hours_ok = sum(i.filing.hours for i in usable)
            coverage[period] = {
                "filings_total": float(len(in_period)),
                "filings_comparable": float(len(usable)),
                "revenue_coverage": (rev_ok / rev_all) if rev_all else 1.0,
                "hours_coverage": (hours_ok / hours_all) if hours_all else 1.0,
            }
        return coverage

    def compatibility(
        self, values_by_metric: dict[str, dict[str, MetricValue]]
    ) -> dict[str, Any]:
        """检查多指标是否共享共同企业与共同期间（共同分母原则）。"""
        populations = {
            metric: {
                enterprise
                for enterprise, value in values.items()
                if value.eligible
            }
            for metric, values in values_by_metric.items()
        }
        windows = {
            metric: self._window(values) for metric, values in values_by_metric.items()
        }
        common = set.intersection(*populations.values()) if populations else set()
        common_window = self._intersect_windows(windows.values())
        compatible = bool(common) and all(
            populations[metric] == common for metric in populations
        ) and common_window is not None
        return {
            "compatible": compatible,
            "populations": {k: sorted(v) for k, v in populations.items()},
            "windows": windows,
            "common_enterprises": sorted(common),
            "common_window": common_window,
            "note": (
                "各指标共享共同可比企业与期间，可形成同组结论"
                if compatible
                else "指标人群或期间不相容，仅分别成立，禁止拼成综合结论"
            ),
        }

    @staticmethod
    def _window(values: dict[str, MetricValue]) -> tuple[str, str] | None:
        periods = [p for value in values.values() if value.eligible for p in value.periods]
        if not periods:
            return None
        return min(periods), max(periods)

    @staticmethod
    def _intersect_windows(windows: Any) -> tuple[str, str] | None:
        windows = [w for w in windows if w is not None]
        if not windows:
            return None
        low = max(w[0] for w in windows)
        high = min(w[1] for w in windows)
        return (low, high) if low <= high else None
