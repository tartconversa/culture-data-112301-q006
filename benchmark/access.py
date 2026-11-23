"""三级接收权限视图。

- public：联盟对外公开表，只含通过披露审查的准确数或达标区间；
- member：成员可见的对标结果，区间最小宽度按 member 政策收紧，且绝不暴露其他
  成员的逐企业明细（private_detail=false）；
- self：企业只能读取自身的申报、修订与项目明细，以及获准的对标信息。

所有对外结构都经本模块裁剪；服务层不直接把内部计算结果交给请求方。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AccessError(PermissionError):
    """请求方无权访问该内容。"""


@dataclass
class AccessPolicy:
    audience: str
    private_detail: bool
    minimum_interval_minor: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AccessPolicy":
        return cls(
            audience=row["id"],
            private_detail=bool(row["private_detail"]),
            minimum_interval_minor=int(row["minimum_interval_minor"]),
        )


class AccessControl:
    def __init__(self, policies: list[dict[str, Any]]):
        self.policies = {
            row["id"]: AccessPolicy.from_row(row) for row in policies
        }

    def public_view(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = {"exact", "interval"}
        return [
            {
                "cell_id": d["cell_id"],
                "metric": d["metric"],
                "period": d["period"],
                "caliber_key": d["caliber_key"],
                "disposition": d["disposition"],
                "published_interval_minor": d["published_interval_minor"],
                "reasons": d["reasons"],
            }
            for d in decisions
            if d["disposition"] in allowed
        ]

    def member_view(
        self, benchmark_result: dict[str, Any], requester: str
    ) -> dict[str, Any]:
        """成员视图：保留组级统计、权重与覆盖，剔除其他企业的可识别明细。"""
        view = {
            "request_id": benchmark_result["request_id"],
            "requester": requester,
            "data_version": benchmark_result["data_version"],
            "window": benchmark_result.get("window"),
            "compatibility": benchmark_result.get("compatibility"),
            "groups": [],
            "own_metrics": benchmark_result.get("own_metrics", {}),
            "exclusions": benchmark_result.get("exclusions", []),
            "identity_warnings": benchmark_result.get("identity_warnings", []),
        }
        for group in benchmark_result["groups"]:
            sanitized = {k: v for k, v in group.items() if k not in {"members", "peer_members"}}
            sanitized["peer_count"] = len(group["peer_members"])
            # 成员列表以匿名计数呈现，避免反推具体企业
            sanitized["member_count"] = len(group["members"])
            view["groups"].append(sanitized)
        return view

    def self_detail(
        self,
        enterprise_id: str,
        requester: str,
        own_filings: list[dict[str, Any]],
        own_projects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if enterprise_id != requester:
            raise AccessError(
                f"企业{requester}无权读取{enterprise_id}的明细（private_detail=false）"
            )
        return {
            "enterprise": enterprise_id,
            "filings": own_filings,
            "projects": own_projects,
        }

    def enforce_ledger_audience(self, entry: dict[str, Any], audience: str) -> bool:
        """public 接收者只能看 public 台账；member 可看 member 与 public。"""
        if audience == "public":
            return entry["audience"] == "public"
        return entry["audience"] in {"public", "member"}
