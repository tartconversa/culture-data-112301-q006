"""可比组构建、统计呈现与放宽建议。

成员查询流程：
1. 按申请的参考市场、业务集合与显式成员范围取交集得到候选企业；
2. 逐指标计算口径可比、观察月达标的企业，形成该指标的可比组；
3. 成员参考分布按指标定义剔除发起方自身（exclude_self），避免企业用自己的数据
   同时抬高行业阈值又证明自己超过阈值；
4. 组内默认等权（每家企业一个指标值），同时给出按外部收入的加权统计与权重表；
5. 单企业贡献占比超过政策 max_share 时标注支配风险，极少数大企业不能在没有
   解释的情况下决定所有成员的排名；
6. 可比组不足 minimum_group 时返回原因与可接受的放宽方向；放宽业务类别或时期
   后重新标注比较含义，且不相容口径不会被自动合并。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .metrics import MetricValue

# 放宽的优先顺序：每一步都伴随比较含义的重新说明
RELAX_STEPS = (
    ("drop_market", "取消参考市场限制，在全联盟同规模层内比较"),
    ("widen_services", "放宽业务类别至相邻类别，比较含义变为综合服务口径"),
    ("extend_window", "向前后各延长一个统计期，覆盖更多观察月"),
    ("drop_size", "取消规模层限制，引入规模差异但保留业务与市场口径"),
)


@dataclass
class PeerGroup:
    metric: str
    members: list[str]
    peer_members: list[str]                 # 按 exclude_self 剔除自身后的参考分布
    requester: str | None
    weights: dict[str, float]
    revenue_weights: dict[str, float]
    statistics: dict[str, Any]
    coverage: dict[str, Any]
    dominance: dict[str, Any]
    sufficient: bool
    minimum_group: int
    reasons: list[str] = field(default_factory=list)
    relaxations: list[dict[str, str]] = field(default_factory=list)
    window: tuple[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "members": self.members,
            "peer_members": self.peer_members,
            "requester": self.requester,
            "weights": self.weights,
            "revenue_weights": self.revenue_weights,
            "statistics": self.statistics,
            "coverage": self.coverage,
            "dominance": self.dominance,
            "sufficient": self.sufficient,
            "minimum_group": self.minimum_group,
            "reasons": self.reasons,
            "relaxations": self.relaxations,
            "window": list(self.window) if self.window else None,
        }


class PeerGroupBuilder:
    def __init__(self, policy_max_share: float, definitions: dict[str, Any]):
        self.max_share = policy_max_share
        self.definitions = definitions

    def build(
        self,
        metric_id: str,
        values: dict[str, MetricValue],
        requester: str | None,
        candidate_members: list[str],
        requested_minimum: int | None = None,
    ) -> PeerGroup:
        definition = self.definitions[metric_id]
        minimum = requested_minimum or definition.minimum_group
        eligible = {
            enterprise: value
            for enterprise, value in values.items()
            if enterprise in set(candidate_members) and value.eligible
        }
        members = sorted(eligible)
        reasons: list[str] = []

        excluded = sorted(set(candidate_members) - set(members))
        if excluded:
            detail = [
                f"{enterprise}（{values[enterprise].eligibility_note if enterprise in values else '无该指标可比申报'}）"
                for enterprise in excluded
            ]
            reasons.append("以下候选企业未进入可比组：" + "、".join(detail))

        peer_members = [m for m in members if not (definition.exclude_self and m == requester)]
        sufficient = len(peer_members) >= minimum
        if not sufficient:
            reasons.append(
                f"参考组企业数 {len(peer_members)} 少于最少要求 {minimum}"
            )

        weights = {m: 1.0 / len(peer_members) for m in peer_members} if peer_members else {}
        revenue_total = sum(eligible[m].numerator_minor for m in peer_members)
        revenue_weights = (
            {
                m: eligible[m].numerator_minor / revenue_total
                for m in peer_members
            }
            if revenue_total > 0
            else {m: 0.0 for m in peer_members}
        )
        statistics = self._statistics(
            peer_members, eligible, weights, revenue_weights, requester
        )
        coverage = self._coverage(peer_members, eligible)
        dominance = self._dominance(peer_members, revenue_weights)
        window = self._window(members, eligible)
        relaxations = [] if sufficient else self._relax_suggestions()
        return PeerGroup(
            metric=metric_id,
            members=members,
            peer_members=peer_members,
            requester=requester,
            weights=weights,
            revenue_weights=revenue_weights,
            statistics=statistics,
            coverage=coverage,
            dominance=dominance,
            sufficient=sufficient,
            minimum_group=minimum,
            reasons=reasons,
            relaxations=relaxations,
            window=window,
        )

    def _statistics(
        self,
        peers: list[str],
        eligible: dict[str, MetricValue],
        weights: dict[str, float],
        revenue_weights: dict[str, float],
        requester: str | None,
    ) -> dict[str, Any]:
        if not peers:
            return {"available": False, "reason": "参考组为空"}
        vals = [eligible[m].value for m in peers]
        ordered = sorted(vals)
        weighted = sum(eligible[m].value * weights[m] for m in peers)
        rev_weighted = sum(eligible[m].value * revenue_weights[m] for m in peers)
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(variance)
        result = {
            "available": True,
            "n": len(peers),
            "equal_weight_mean": round(weighted, 6),
            "revenue_weighted_mean": round(rev_weighted, 6),
            "median": round(self._quantile(ordered, 0.5), 6),
            "q25": round(self._quantile(ordered, 0.25), 6),
            "q75": round(self._quantile(ordered, 0.75), 6),
            "min": round(ordered[0], 6),
            "max": round(ordered[-1], 6),
            "std": round(std, 6),
            # 用样本标准误刻画参考分布的不确定性，组越小区间越宽
            "sem": round(std / math.sqrt(len(peers)), 6) if len(peers) > 1 else None,
        }
        if requester in eligible and eligible[requester].eligible:
            own = eligible[requester].value
            below = sum(1 for v in vals if v < own)
            result["requester_value"] = round(own, 6)
            result["requester_percentile"] = round(
                100.0 * below / len(peers), 2
            ) if peers else None
        return result

    @staticmethod
    def _quantile(ordered: list[float], q: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        pos = q * (len(ordered) - 1)
        low = int(math.floor(pos))
        high = int(math.ceil(pos))
        if low == high:
            return ordered[low]
        return ordered[low] * (high - pos) + ordered[high] * (pos - low)

    def _coverage(
        self, peers: list[str], eligible: dict[str, MetricValue]
    ) -> dict[str, Any]:
        if not peers:
            return {"available": False}
        period_rev: dict[str, list[float]] = {}
        for member in peers:
            for period, cov in eligible[member].per_period_coverage.items():
                period_rev.setdefault(period, []).append(cov["revenue_coverage"])
        return {
            "available": True,
            "period_revenue_coverage": {
                period: round(sum(vals) / len(vals), 4)
                for period, vals in sorted(period_rev.items())
            },
            "mean_revenue_coverage": round(
                sum(
                    sum(vals) / len(vals)
                    for vals in period_rev.values()
                )
                / max(len(period_rev), 1),
                4,
            ),
        }

    def _dominance(self, peers: list[str], revenue_weights: dict[str, float]) -> dict[str, Any]:
        if not peers:
            return {"flagged": False}
        top = max(revenue_weights, key=revenue_weights.get)
        share = revenue_weights[top]
        return {
            "flagged": share > self.max_share,
            "top_enterprise": top,
            "top_revenue_share": round(share, 4),
            "threshold": self.max_share,
            "explanation": (
                f"最大企业{top}占组内外部收入{share:.1%}，超过政策阈值{self.max_share:.0%}，"
                "等权统计与排名需随附该支配说明"
                if share > self.max_share
                else "无单企业支配风险"
            ),
        }

    @staticmethod
    def _window(members: list[str], eligible: dict[str, MetricValue]) -> tuple[str, str] | None:
        periods = [p for m in members for p in eligible[m].periods]
        return (min(periods), max(periods)) if periods else None

    @staticmethod
    def _relax_suggestions() -> list[dict[str, str]]:
        return [
            {"step": step, "meaning": meaning}
            for step, meaning in RELAX_STEPS
        ]
