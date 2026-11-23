"""联盟披露审查与受限结果发布。

政策（data/disclosure_policy.jsonl 与 docs/资料契约.md）：
- 准确值只有同时满足 minimum_contributors 与 max_share 才可公开；
- 贡献不足或过度集中的格只能公开宽度至少 minimum_interval_minor 的区间；
  准确值视为宽度零；
- 与既有发布（准确数、区间、合计）以及本次申请的其他格组合后，任何同类不安全
  细格可被推导出的区间也必须保留该最小宽度——即“相减识别”防护；
- 无法安全发布的细格可以合并或扩大区间，但不能改动已公开事实，也不能让不相容
  口径的合并看起来是精确结果。

实现要点：
- 每个待审格先按自身贡献者数与集中度判定；
- 再把同 metric、同统计期窗口、口径相容的历史/本次格写成线性约束，用线性规划
  求该格真实总量在所有约束下的可行区间（LP 松弛，给出最宽可行界，安全一侧）；
- 可行区间窄于 minimum_interval_minor 时，只能发布不窄于该宽度且覆盖可行区间
  的公版区间；若连与历史事实相容的宽区间都无法表达新信息，则拒绝新增发布。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # scipy 在 requirements.txt 中固定；缺失时降级到纯标准库传播
    from scipy.optimize import linprog
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - 环境缺依赖时的降级路径
    _HAS_SCIPY = False


@dataclass(frozen=True)
class KnownCell:
    """审查时作为约束的已知格（历史发布或本次同批申请的其他格）。"""

    id: str
    members: frozenset[str]
    metric: str
    period: str
    lower_minor: int
    upper_minor: int
    audience: str
    caliber_key: str
    services: frozenset[str]
    historical: bool

    @property
    def exact(self) -> bool:
        return self.lower_minor == self.upper_minor


@dataclass
class CellDecision:
    cell_id: str
    metric: str
    period: str
    members: list[str]
    caliber_key: str
    services: list[str]
    true_value_minor: int | None
    contributor_count: int
    top_share: float | None
    own_cell_safe: bool
    derived_lower: int
    derived_upper: int
    derived_width: int
    policy_min_interval: int
    disposition: str = ""  # exact / interval / merge / deny
    publish_lower: int | None = None
    publish_upper: int | None = None
    reasons: list[str] = field(default_factory=list)
    constraints_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "metric": self.metric,
            "period": self.period,
            "members": self.members,
            "caliber_key": self.caliber_key,
            "services": list(self.services),
            "true_value_minor": self.true_value_minor,
            "contributor_count": self.contributor_count,
            "top_share": None if self.top_share is None else round(self.top_share, 4),
            "own_cell_safe": self.own_cell_safe,
            "derived_interval_minor": [self.derived_lower, self.derived_upper],
            "derived_width": None if self.derived_width < 0 else self.derived_width,
            "policy_min_interval_minor": self.policy_min_interval,
            "disposition": self.disposition,
            "published_interval_minor": (
                [self.publish_lower, self.publish_upper]
                if self.publish_lower is not None
                else None
            ),
            "reasons": self.reasons,
            "constraints_used": self.constraints_used,
        }


class DisclosureAuditor:
    def __init__(self, policy: dict[str, Any], use_lp: bool = True):
        self.policy = policy
        self.min_contributors = int(policy["minimum_contributors"])
        self.max_share = float(policy["max_share"])
        self.min_interval = int(policy["minimum_interval_minor"])
        self.use_lp = use_lp and _HAS_SCIPY

    def audit(
        self,
        cell_id: str,
        metric: str,
        period: str,
        members: list[str],
        caliber_key: str,
        services: frozenset[str],
        true_value_minor: int | None,
        contributions: dict[str, int],
        known_cells: list[KnownCell],
        merge_partners: list[str] | None = None,
    ) -> CellDecision:
        member_set = frozenset(members)
        # 贡献者按实际有正贡献的企业计；范围内没有该业务申报的企业不抬高贡献者数
        positive = {m: c for m, c in contributions.items() if m in member_set and c > 0}
        contributor_count = len(positive)
        total = sum(positive.values())
        top_share = (
            max(positive.values()) / total
            if total > 0
            else None
        )
        own_safe = (
            contributor_count >= self.min_contributors
            and top_share is not None
            and top_share <= self.max_share
        )
        reasons: list[str] = []
        if contributor_count < self.min_contributors:
            reasons.append(
                f"实际贡献企业数{contributor_count}少于准确值最少要求{self.min_contributors}"
                f"（申请范围{len(member_set)}家中{len(member_set) - contributor_count}家无该业务可比贡献）"
            )
        if top_share is not None and top_share > self.max_share:
            reasons.append(
                f"最大单企业贡献比{top_share:.1%}超过阈值{self.max_share:.0%}"
            )

        # 相容约束：同指标、同统计期、归属基准相容（服务范围必须包含本格服务集合，
        # 全服务历史格与服务子集格因此可组合做相减推导）。不相容口径不进入约束。
        compatible = [
            cell
            for cell in known_cells
            if cell.metric == metric
            and cell.period == period
            and services <= cell.services
        ]
        low, high, used, bounded = self._derivable_bounds(member_set, compatible)
        # 收入非负
        low = max(0, low)
        # 外部约束给出的可行宽度单独保留：它衡量“仅凭已发布信息能推出多窄”，
        # 不被本次申请持有的真实值撑大，否则会掩盖相减识别风险。
        # 无界方向（bounded=False）视为无限宽，用 None 表示。
        external_width = max(0, high - low) if bounded else None
        # 对外呈现的可行区间取外部界与真实值的并集：必须覆盖本格真实值，
        # 而政策判定仍使用不含真实值的 external_width。无外部界时仅以真值为锚点。
        if not bounded:
            feasible_low = 0
            feasible_high = true_value_minor if true_value_minor is not None else 0
        else:
            feasible_low, feasible_high = low, high
            if true_value_minor is not None:
                feasible_low = min(low, true_value_minor)
                feasible_high = max(high, true_value_minor)
        derived_width = external_width

        decision = CellDecision(
            cell_id=cell_id,
            metric=metric,
            period=period,
            members=sorted(member_set),
            caliber_key=caliber_key,
            services=sorted(services),
            true_value_minor=true_value_minor,
            contributor_count=contributor_count,
            top_share=top_share,
            own_cell_safe=own_safe,
            derived_lower=feasible_low,
            derived_upper=feasible_high,
            derived_width=derived_width if bounded else -1,
            policy_min_interval=self.min_interval,
            reasons=reasons,
            constraints_used=used,
        )

        # 判定顺序：
        # 1) 自身达标且没有任何相容历史/同批约束：无外部推导路径，准确发布；
        # 2) 自身达标且约束给出有限、不窄于政策下限的可行区间：准确发布；
        # 3) 自身达标但约束无界或把可行域压窄到下限以下：存在推导风险，改区间；
        # 4) 自身不达标：只能走区间/合并/拒绝策略。
        has_external_constraint = bool(compatible)
        if own_safe and not has_external_constraint:
            decision.disposition = "exact"
            decision.publish_lower = decision.publish_upper = true_value_minor
            decision.reasons.append("自身贡献与集中度达标，且无相容外部约束")
            return decision

        if own_safe and bounded and derived_width >= self.min_interval:
            decision.disposition = "exact"
            decision.publish_lower = decision.publish_upper = true_value_minor
            decision.reasons.append("自身贡献与集中度达标，外部可行区间不窄于政策下限")
            return decision

        if own_safe:
            decision.reasons.append(
                f"本格自身达标，但相容发布组合把可行域压窄到{derived_width}"
                f"（小于最小宽度{self.min_interval}），存在相减识别风险，改以区间发布"
            )

        return self._apply_interval_policy(
            decision, merge_partners, has_external_constraint, feasible_low, feasible_high
        )

    def _apply_interval_policy(
        self,
        decision: CellDecision,
        merge_partners: list[str] | None,
        has_external_constraint: bool,
        feasible_low: int,
        feasible_high: int,
    ) -> CellDecision:
        if not has_external_constraint:
            # 没有外部约束界定可行域：有真实值时围绕真实值构造最小宽度区间；
            # 无真实值（成员无该业务贡献）时不存在有信息量的区间，拒绝或建议合并。
            if decision.true_value_minor is None:
                if merge_partners:
                    decision.disposition = "merge"
                    decision.reasons.append(
                        "无相容历史约束可界定可行区间，建议与相邻格合并后重审"
                    )
                    return decision
                decision.disposition = "deny"
                decision.reasons.append(
                    "无相容历史约束且无可用真实值，无法构造有信息量的区间，拒绝新增发布"
                )
                return decision
            return self._widen_around_true_value(
                decision, feasible_low, feasible_high, has_external_constraint
            )

        if decision.derived_width >= self.min_interval:
            decision.disposition = "interval"
            decision.publish_lower = feasible_low
            decision.publish_upper = feasible_high
            decision.reasons.append(
                f"可推导区间宽度{decision.derived_width}已满足最小宽度{self.min_interval}"
            )
            return decision

        if decision.true_value_minor is not None:
            return self._widen_around_true_value(
                decision, feasible_low, feasible_high, has_external_constraint
            )

        if merge_partners:
            decision.disposition = "merge"
            decision.reasons.append(
                "单格无法安全发布，建议与相邻格合并；合并仅可作为区间且必须保持同口径"
            )
            return decision

        decision.disposition = "deny"
        decision.reasons.append(
            "无法在不改动已公开事实的前提下构造满足最小宽度的区间，拒绝新增发布"
        )
        return decision

    def _widen_around_true_value(
        self,
        decision: CellDecision,
        feasible_low: int,
        feasible_high: int,
        has_external_constraint: bool,
    ) -> CellDecision:
        """以真实值为中心构造宽度恰为政策下限、下界非负且覆盖可行域的公版区间。"""
        width = self.min_interval
        center = decision.true_value_minor
        candidate_low = max(0, center - width // 2)
        candidate_high = candidate_low + width
        if has_external_constraint:
            # 存在外部可行域时，区间必须覆盖整个可行域，再夹紧非负下界
            if candidate_high < feasible_high:
                candidate_high = feasible_high
                candidate_low = max(0, candidate_high - width)
                candidate_high = candidate_low + width
            if candidate_low > feasible_low:
                candidate_low = max(0, feasible_low)
                candidate_high = candidate_low + width
            covers = candidate_low <= feasible_low and candidate_high >= feasible_high
        else:
            # 无外部约束：只需包含真实值且下界非负
            covers = candidate_low <= center <= candidate_high
        if covers:
            decision.disposition = "interval"
            decision.publish_lower = candidate_low
            decision.publish_upper = candidate_high
            decision.reasons.append(
                f"外部可行宽度{decision.derived_width}低于最小宽度{width}，"
                "围绕真实值扩大到政策最小宽度，下界非负"
                + ("且覆盖全部可行值" if has_external_constraint else "（无外部约束）")
            )
            return decision
        decision.disposition = "deny"
        decision.reasons.append(
            "无法在不改动已公开事实的前提下构造满足最小宽度的区间，拒绝新增发布"
        )
        return decision

    # ---------------------------------------------------------- 推导引擎
    def _derivable_bounds(
        self, target: frozenset[str], cells: list[KnownCell]
    ) -> tuple[int, int, list[str], bool]:
        if not cells:
            return 0, 0, [], False
        used = [cell.id for cell in cells]
        if self.use_lp:
            low, high, bounded = self._lp_bounds(target, cells)
            if low is not None and high is not None:
                return low, high, used, bounded
        low, high, bounded = self._propagation_bounds(target, cells)
        return low, high, used, bounded

    def _lp_bounds(
        self, target: frozenset[str], cells: list[KnownCell]
    ) -> tuple[int | None, int | None, bool]:
        universe = sorted(set().union(*(cell.members for cell in cells), target))
        index = {member: i for i, member in enumerate(universe)}
        n = len(universe)
        # 约束 A_ub x <= b_ub：每个已知格给出上下两条
        a_ub: list[list[float]] = []
        b_ub: list[float] = []
        for cell in cells:
            row_pos = [0.0] * n
            for member in cell.members:
                row_pos[index[member]] = 1.0
            a_ub.append(row_pos)
            b_ub.append(float(cell.upper_minor))
            a_ub.append([-v for v in row_pos])
            b_ub.append(-float(cell.lower_minor))
        target_row = [0.0] * n
        for member in target:
            target_row[index[member]] = 1.0
        bounds = [(0, None)] * n

        def solve(objective: list[float]) -> tuple[float | None, bool]:
            """返回（最优值, 是否无界）；不可行时最优值为 None。"""
            result = linprog(
                objective,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=bounds,
                method="highs",
            )
            if result.success:
                return float(result.fun), False
            # HiGHS 无界时 success=False，status==3
            if result.status == 3 or "unbounded" in (result.message or "").lower():
                return None, True
            return None, False

        # solve(target_row) 给出目标最小值（下界）；
        # solve(-target_row) 给出负的目标最大值，取负后为上界。
        min_value, min_unbounded = solve(target_row)
        neg_max_value, max_unbounded = solve([-v for v in target_row])
        if min_value is None and not min_unbounded:
            return None, None, False  # 不可行，交给传播降级
        low_value = 0 if min_unbounded else int(round(min_value))
        if max_unbounded:
            # 上界无界：下界仍有效，用 None 上界表达“不有界”，避免把无界误当成 0
            return low_value, None, False
        high_value = int(round(-neg_max_value))
        return low_value, high_value, True

    def _propagation_bounds(
        self, target: frozenset[str], cells: list[KnownCell]
    ) -> tuple[int, int, bool]:
        """无 scipy 时的纯标准库降级：区间算术 + 集合包含/差分传播。

        覆盖合计、子集相减等本项目出现的推导结构；不如 LP 完整，但结果始终
        落在可行域内（只可能偏宽，不会泄露更窄的信息）。无任何有限上界时
        显式返回 bounded=False，不使用占位大数。
        """
        low = 0
        high: int | None = None
        for cell in cells:
            if cell.members == target:
                low = max(low, cell.lower_minor)
                high = cell.upper_minor if high is None else min(high, cell.upper_minor)
        # 多轮传播直到稳定
        for _ in range(len(cells) + 2):
            changed = False
            for cell in cells:
                if target < cell.members:
                    rest = cell.members - target
                    rest_low, rest_high = self._rest_bounds(rest, cells)
                    new_low = max(low, cell.lower_minor - (rest_high or 10**15))
                    if high is None:
                        if rest_high is not None:
                            new_high: int | None = cell.upper_minor - rest_low
                        else:
                            new_high = None
                    else:
                        new_high = min(high, cell.upper_minor - rest_low)
                    if (new_low, new_high) != (low, high):
                        low, high = new_low, new_high
                        changed = True
            if not changed:
                break
        bounded = high is not None
        return low, (high if bounded else 0), bounded

    def _rest_bounds(
        self, members: frozenset[str], cells: list[KnownCell]
    ) -> tuple[int, int | None]:
        low = 0
        high: int | None = None
        for cell in cells:
            if cell.members == members:
                low = max(low, cell.lower_minor)
                high = cell.upper_minor if high is None else min(high, cell.upper_minor)
        return low, high
