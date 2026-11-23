"""对标应用编排层。

把资料加载、修订台账、口径标注、收入归属、指标计算、可比组、披露审查与权限
视图组合成两条业务流程：
- benchmark：成员对标查询（含可比组构成、权重、覆盖、不确定性与放宽建议）；
- publish：联盟发布申请审查（含历史组合推导、最小公开策略与版本漂移说明）。

所有输出都绑定口径键与数据版本。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .access import AccessControl
from .attribution import AttributionEngine
from .calibers import CaliberRegistry
from .disclosure import DisclosureAuditor, KnownCell
from .ledger import PublicationLedger
from .materials import MaterialStore
from .metrics import MetricEngine
from .peergroup import PeerGroupBuilder
from .revisions import RevisionLedger


@dataclass
class BenchmarkContext:
    store: MaterialStore
    revisions: RevisionLedger
    ledger: PublicationLedger
    calibers: CaliberRegistry
    metrics: MetricEngine
    attribution: AttributionEngine
    access: AccessControl
    as_of_period: str
    identity_mismatches: list[dict[str, Any]]

    @property
    def data_version(self) -> str:
        return self.revisions.data_version()


def build_context(
    root: str,
    state_dir: str,
    as_of_period: str | None = None,
    use_lp: bool = True,
) -> BenchmarkContext:
    store = MaterialStore.load(root)
    revisions = RevisionLedger.open(store, state_dir)
    ledger = PublicationLedger.open(
        state_dir, seed_cells=store.rows("published_cells")
    )
    calibers = CaliberRegistry(store.rows("definition_changes"))
    if as_of_period is None:
        as_of_period = max(f["period"] for f in store.rows("filings"))
    metrics = MetricEngine(
        store.rows("definitions"), store.by_id("enterprises"), as_of_period
    )
    effective = revisions.effective_filings()
    assessments = calibers.assessments(effective)
    attribution = AttributionEngine(store, assessments)
    attributed = attribution.attribute_all(effective)
    mismatches = attribution.check_identity(attributed)
    # 基础资料受指纹保护，未接收运行期修订时恒等式必须零偏差
    if not revisions.incoming_filings and not any(
        event.source == "state" for event in revisions.events
    ) and mismatches:
        raise ValueError(f"基础资料归属恒等式不成立：{mismatches}")
    access = AccessControl(store.rows("disclosure_policy"))
    return BenchmarkContext(
        store=store,
        revisions=revisions,
        ledger=ledger,
        calibers=calibers,
        metrics=metrics,
        attribution=attribution,
        access=access,
        as_of_period=as_of_period,
        identity_mismatches=mismatches,
    )


class BenchmarkService:
    def __init__(self, ctx: BenchmarkContext):
        self.ctx = ctx
        self._attributed = ctx.attribution.attribute_all(
            ctx.revisions.effective_filings()
        )
        self._enterprises = ctx.store.by_id("enterprises")
        policy_member = next(
            p for p in ctx.store.rows("disclosure_policy") if p["id"] == "member"
        )
        self._builder = PeerGroupBuilder(
            policy_max_share=float(policy_member["max_share"]),
            definitions=ctx.metrics.definitions,
        )

    # ------------------------------------------------------------- 成员对标
    def benchmark(self, request: dict[str, Any]) -> dict[str, Any]:
        requester = request["requester"]
        if requester != "alliance" and requester not in self._enterprises:
            raise ValueError(f"发起方不存在：{requester}")
        services = set(request["services"])
        candidates = self._candidate_members(request)
        scope = self._scope_items(services)
        window = self._window(scope)
        caliber_key = self._caliber_key(services, window)

        values_by_metric: dict[str, dict[str, Any]] = {}
        own_metrics: dict[str, Any] = {}
        for metric_id in request["metrics"]:
            values = {}
            for enterprise in candidates:
                items = [
                    item
                    for item in scope
                    if item.filing.enterprise == enterprise
                ]
                values[enterprise] = self.ctx.metrics.evaluate(
                    enterprise, metric_id, items
                )
            values_by_metric[metric_id] = values
            if requester in values:
                own_metrics[metric_id] = values[requester].to_dict()

        groups = [
            self._builder.build(
                metric_id,
                values_by_metric[metric_id],
                requester if requester != "alliance" else None,
                candidates,
            ).to_dict()
            for metric_id in request["metrics"]
        ]
        compatibility = self.ctx.metrics.compatibility(values_by_metric)
        exclusions = self.ctx.attribution.explain_exclusions(scope)
        result = {
            "request_id": request["id"],
            "kind": "benchmark",
            "requester": requester,
            "market": request["market"],
            "services": sorted(services),
            "metrics": list(request["metrics"]),
            "window": list(window) if window else None,
            "caliber_key": caliber_key,
            "data_version": self.ctx.data_version,
            "groups": groups,
            "compatibility": compatibility,
            "own_metrics": own_metrics,
            "exclusions": exclusions,
            "identity_warnings": self.ctx.identity_mismatches,
        }
        return result

    # ------------------------------------------------------------- 发布审查
    def review_publication(self, request: dict[str, Any], today: str) -> dict[str, Any]:
        """审查发布申请。

        申请可在 ``cells`` 中给出多组交叉维度（每组各自的成员子集与服务集合）；
        单张表达标的格即使自身满足最少企业数，也要与同批其他格一起做组合推导
        审查（两轮：第一轮按历史约束判定，第二轮把同批拟发布格作为追加约束复核）。
        未提供 ``cells`` 时按申请顶层 members/services/metrics 生成单一格。
        """
        policy_row = next(
            p for p in self.ctx.store.rows("disclosure_policy") if p["id"] == "public"
        )
        auditor = DisclosureAuditor(policy_row)
        cells_spec = self._cell_specs(request)
        for spec in cells_spec:
            unknown = sorted(set(spec["members"]) - set(self._enterprises))
            if unknown:
                raise ValueError(f"申请范围含未知企业：{unknown}")
        drift = self.ctx.ledger.check_version_drift(
            self.ctx.data_version, request.get("data_version")
        )

        historical = self.ctx.ledger.known_cells(include_withdrawn=True)
        first_pass = [
            decision
            for spec in cells_spec
            for decision in self._audit_cell(
                auditor, request["id"], spec, historical, cells_spec
            )
        ]
        # 第二轮：把同批拟发布（exact/interval）格作为追加约束，检查组合推导。
        # 按 cell_id 去重，且同一格不作为自身的追加约束。
        batch_by_id: dict[str, KnownCell] = {}
        for d in first_pass:
            if d.disposition in {"exact", "interval"} and d.publish_lower is not None:
                batch_by_id[d.cell_id] = KnownCell(
                    id=d.cell_id,
                    members=frozenset(d.members),
                    metric=d.metric,
                    period=d.period,
                    lower_minor=d.publish_lower,
                    upper_minor=d.publish_upper,
                    audience="public",
                    caliber_key=d.caliber_key,
                    services=frozenset(d.services),
                    historical=False,
                )
        decisions = []
        for spec in cells_spec:
            baseline = self._audit_cell(auditor, request["id"], spec, historical, cells_spec)
            for first in baseline:
                others = [
                    cell
                    for cid, cell in batch_by_id.items()
                    if cid != first.cell_id
                ]
                second = self._audit_cell(
                    auditor, request["id"], spec, historical + others, cells_spec
                )
                second = next(d for d in second if d.cell_id == first.cell_id)
                first_width = first.derived_width
                second_width = second.derived_width
                tightened = (
                    first_width is not None
                    and second_width is not None
                    and second_width < first_width
                ) or (first_width is None and second_width is not None)
                if tightened or second.disposition != first.disposition:
                    second.reasons.append(
                        "同批其他格与该格可组合相减，已按追加约束收紧/改判"
                    )
                if second.disposition not in {"exact", "interval"}:
                    second.reasons.append(
                        "可与申请内其他交叉维度合并后重新提交，合并格须保持同口径且以区间呈现"
                    )
                decisions.append(second.to_dict())

        primary_window = self._window(self._scope_items(set(request["services"])))
        return {
            "request_id": request["id"],
            "kind": "publish",
            "requester": request["requester"],
            "services": sorted(request["services"]),
            "metrics": list(request["metrics"]),
            "window": list(primary_window) if primary_window else None,
            "data_version": self.ctx.data_version,
            "version_drift": drift,
            "decisions": decisions,
            "public_view": self.ctx.access.public_view(decisions),
            "identity_warnings": self.ctx.identity_mismatches,
        }

    def _cell_specs(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        if request.get("cells"):
            specs = []
            for index, cell in enumerate(request["cells"]):
                specs.append(
                    {
                        "tag": cell.get("tag", f"cell-{index}"),
                        "members": list(cell["members"]),
                        "services": list(cell.get("services", request["services"])),
                        "metrics": list(cell.get("metrics", request["metrics"])),
                    }
                )
            return specs
        return [
            {
                "tag": "primary",
                "members": list(request["members"]),
                "services": list(request["services"]),
                "metrics": list(request["metrics"]),
            }
        ]

    def _audit_cell(
        self,
        auditor: "DisclosureAuditor",
        request_id: str,
        spec: dict[str, Any],
        known: list[KnownCell],
        all_specs: list[dict[str, Any]],
    ) -> list:
        services = set(spec["services"])
        members = list(spec["members"])
        scope = self._scope_items(services)
        window = self._window(scope)
        caliber_key = self._caliber_key(services, window)
        merge_partners = [s["tag"] for s in all_specs if s["tag"] != spec["tag"]]
        results = []
        id_prefix = request_id if len(all_specs) == 1 else request_id + ":" + spec["tag"]
        for metric_id in spec["metrics"]:
            cell_id = self._cell_id(id_prefix, metric_id, members, window)
            contributions = self._contributions(members, scope, metric_id)
            true_value = sum(contributions.values()) if contributions else None
            results.append(
                auditor.audit(
                    cell_id=cell_id,
                    metric=metric_id,
                    period=f"{window[0]}/{window[1]}" if window else "n/a",
                    members=members,
                    caliber_key=caliber_key,
                    services=frozenset(services),
                    true_value_minor=true_value,
                    contributions=contributions,
                    known_cells=known,
                    merge_partners=merge_partners or None,
                )
            )
        return results

    def approve(self, review: dict[str, Any], today: str) -> list[dict[str, Any]]:
        """把审查通过（exact/interval）的格写入只追加台账。"""
        published = []
        for decision in review["decisions"]:
            if decision["disposition"] not in {"exact", "interval"}:
                continue
            entry = self.ctx.ledger.publish(
                cell_id=decision["cell_id"],
                members=decision["members"],
                metric=decision["metric"],
                period=decision["period"],
                lower_minor=decision["published_interval_minor"][0],
                upper_minor=decision["published_interval_minor"][1],
                audience="public",
                caliber_key=decision["caliber_key"],
                services=decision["services"],
                data_version=review["data_version"],
                published_on=today,
            )
            published.append(entry)
        return published

    # ------------------------------------------------------------- 自身明细
    def self_detail(self, enterprise_id: str, requester: str) -> dict[str, Any]:
        filings = [
            {
                "filing": item.filing.id,
                "project": item.filing.project,
                "period": item.filing.period,
                "service": item.filing.service,
                "basis": item.filing.basis,
                "declared_revenue_minor": item.filing.base_revenue_minor,
                "effective_revenue_minor": item.filing.effective_revenue_minor,
                "external_revenue_minor": item.external_revenue_minor,
                "hours": item.filing.hours,
                "role": item.role,
                "comparability": item.assessment.comparability.value,
                "caliber_reason": item.assessment.reason,
                "revision_history": [e.to_row() for e in item.filing.history],
            }
            for item in self._attributed.values()
            if item.filing.enterprise == enterprise_id
        ]
        project_ids = {row["project"] for row in filings}
        projects = [
            self.ctx.store.by_id("projects")[pid]
            for pid in sorted(project_ids)
        ]
        return self.ctx.access.self_detail(
            enterprise_id, requester, filings, projects
        )

    # ------------------------------------------------------------- 内部工具
    def _candidate_members(self, request: dict[str, Any]) -> list[str]:
        requested = list(request["members"])
        if request.get("market", "all") == "all":
            pool = set(self._enterprises)
        else:
            pool = {
                eid
                for eid, row in self._enterprises.items()
                if row["market"] == request["market"]
            }
        members = sorted(set(requested) & pool)
        return members

    def _scope_items(self, services: set[str]) -> list[Any]:
        return [
            item
            for item in self._attributed.values()
            if item.filing.service in services
        ]

    @staticmethod
    def _window(scope: list[Any]) -> tuple[str, str] | None:
        periods = {item.filing.period for item in scope}
        return (min(periods), max(periods)) if periods else None

    @staticmethod
    def _caliber_key(services: set[str], window: tuple[str, str] | None) -> str:
        # 族键只表达归属基准与期间，服务范围另以 services 字段参与相容性判断：
        # 全服务格与服务子集格同属 external-net 族，可在同期间内组合推导。
        period_key = f"{window[0]}..{window[1]}" if window else "no-window"
        return f"external-net/{period_key}"

    def _contributions(
        self, members: list[str], scope: list[Any], metric_id: str
    ) -> dict[str, int]:
        usable = [
            item
            for item in scope
            if item.filing.enterprise in set(members) and item.usable
        ]
        if metric_id == "rev-contract":
            usable = [item for item in usable if item.project_completed]
        contributions: dict[str, int] = {}
        for item in usable:
            eid = item.filing.enterprise
            contributions[eid] = contributions.get(eid, 0) + item.external_revenue_minor
        return contributions

    @staticmethod
    def _cell_id(
        request_id: str, metric_id: str, members: list[str], window: tuple[str, str] | None
    ) -> str:
        raw = f"{request_id}|{metric_id}|{','.join(sorted(members))}|{window}"
        return "CELL-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
