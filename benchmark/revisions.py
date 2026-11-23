"""申报接收与修订/撤回台账。

设计原则：
1. 基础申报（data/filings.jsonl）受资料指纹保护，不可改写；新到申报与修订只能
   追加到 state/ 下的只追加文件，保留完整到达轨迹。
2. 修订按企业申报的版本号顺序生效；``replace`` 替换当前声明收入，
   ``withdraw_revision`` 撤回此前修订、恢复到被修订申报的原值，但事件本身不删除。
3. 撤回不能让已经对外披露的信息“从未发生”：本模块只负责计算当前有效值并保留
   全部历史事件，发布台账（ledger）另有不可变约束。
4. 每次状态变化都会改变状态指纹，成员分析与发布申请都绑定该指纹，审批时可据此
   发现版本漂移。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .materials import MaterialStore

VALID_ACTIONS = frozenset({"replace", "withdraw_revision"})
SERVICES = frozenset({"publishing", "localization", "design", "support"})
BASES = frozenset({"gross-prime", "subcontract"})


class RevisionError(ValueError):
    """修订链或新申报不合法。"""


@dataclass(frozen=True)
class RevisionEvent:
    id: str
    filing: str
    version: int
    revenue_minor: int
    received_on: str
    action: str
    source: str  # material（随资料到达）或 state（运行期接收）

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filing": self.filing,
            "version": self.version,
            "revenue_minor": self.revenue_minor,
            "received_on": self.received_on,
            "action": self.action,
        }


@dataclass(frozen=True)
class EffectiveFiling:
    """一份申报在当前修订状态下的有效视图。"""

    id: str
    enterprise: str
    project: str
    period: str
    service: str
    hours: float
    basis: str
    base_version: int
    base_revenue_minor: int
    effective_revenue_minor: int
    current_revision: str | None
    withdrawn: bool
    history: tuple[RevisionEvent, ...]
    received_on: str
    source: str

    @property
    def effective_version(self) -> int:
        if self.current_revision is None:
            return self.base_version
        return self.history[-1].version


@dataclass
class RevisionLedger:
    store: MaterialStore
    state_dir: Path
    events: list[RevisionEvent] = field(default_factory=list)
    incoming_filings: list[dict[str, Any]] = field(default_factory=list)
    state_fingerprint: str = ""

    @classmethod
    def open(cls, store: MaterialStore, state_dir: str | Path) -> "RevisionLedger":
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        events = [
            RevisionEvent(source="material", **row)
            for row in store.rows("revisions")
        ]
        rev_path = state_dir / "incoming_revisions.jsonl"
        if rev_path.exists():
            for line in rev_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    events.append(RevisionEvent(source="state", **row))
        filings_path = state_dir / "incoming_filings.jsonl"
        incoming: list[dict[str, Any]] = []
        if filings_path.exists():
            for line in filings_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    incoming.append(json.loads(line))
        ledger = cls(store, state_dir, events, incoming)
        ledger._validate_chain()
        ledger._validate_incoming_filings()
        ledger.state_fingerprint = ledger._compute_fingerprint()
        return ledger

    # ------------------------------------------------------------------ 接收
    def receive_filing(self, row: dict[str, Any]) -> None:
        """接收一份新的逐项目月度申报（仅允许 state 追加，不得覆盖资料申报）。"""
        required = {
            "id", "enterprise", "project", "period", "service",
            "revenue_minor", "hours", "basis", "version", "received_on",
        }
        if set(row) != required:
            raise RevisionError(f"申报字段不符：{sorted(set(row) ^ required)}")
        if row["id"] in {f["id"] for f in self.store.rows("filings")}:
            raise RevisionError(f"申报标识已存在于基础资料：{row['id']}")
        if row["id"] in {f["id"] for f in self.incoming_filings}:
            raise RevisionError(f"申报标识重复接收：{row['id']}")
        self._check_filing_references(row)
        if row["service"] not in SERVICES or row["basis"] not in BASES:
            raise RevisionError("业务类别或申报口径不合法")
        if row["version"] != 1:
            raise RevisionError("新申报首版必须为 version=1，后续修改请走修订")
        self._append_jsonl("incoming_filings.jsonl", row)
        self.incoming_filings.append(dict(row))
        self._refresh()

    def receive_revision(self, row: dict[str, Any]) -> None:
        """接收一条修订或撤回。"""
        required = {"id", "filing", "version", "revenue_minor", "received_on", "action"}
        if set(row) != required:
            raise RevisionError(f"修订字段不符：{sorted(set(row) ^ required)}")
        if row["action"] not in VALID_ACTIONS:
            raise RevisionError(f"修订动作不合法：{row['action']}")
        if row["id"] in {e.id for e in self.events}:
            raise RevisionError(f"修订标识重复：{row['id']}")
        known = {f["id"] for f in self._all_filing_rows()}
        if row["filing"] not in known:
            raise RevisionError(f"被修订申报不存在：{row['filing']}")
        max_version = max(
            (e.version for e in self.events if e.filing == row["filing"]),
            default=1,
        )
        if row["version"] <= max_version:
            raise RevisionError(
                f"修订版本必须递增：{row['filing']} 已有版本 {max_version}"
            )
        date.fromisoformat(row["received_on"])
        event = RevisionEvent(source="state", **row)
        self._append_jsonl("incoming_revisions.jsonl", event.to_row())
        self.events.append(event)
        self._refresh()

    # ------------------------------------------------------------------ 视图
    def effective_filings(self) -> dict[str, EffectiveFiling]:
        by_filing: dict[str, list[RevisionEvent]] = {}
        for event in sorted(self.events, key=lambda e: (e.filing, e.version)):
            by_filing.setdefault(event.filing, []).append(event)
        result: dict[str, EffectiveFiling] = {}
        for row in self._all_filing_rows():
            history = tuple(by_filing.get(row["id"], ()))
            effective = row["revenue_minor"]
            current: str | None = None
            withdrawn = False
            for event in history:
                if event.action == "replace":
                    effective = event.revenue_minor
                    current = event.id
                    withdrawn = False
                else:  # withdraw_revision：恢复被修订申报原值
                    effective = row["revenue_minor"]
                    current = None
                    withdrawn = True
            result[row["id"]] = EffectiveFiling(
                id=row["id"],
                enterprise=row["enterprise"],
                project=row["project"],
                period=row["period"],
                service=row["service"],
                hours=float(row["hours"]),
                basis=row["basis"],
                base_version=row["version"],
                base_revenue_minor=row["revenue_minor"],
                effective_revenue_minor=effective,
                current_revision=current,
                withdrawn=withdrawn,
                history=history,
                received_on=row["received_on"],
                source="material" if any(
                    f["id"] == row["id"] for f in self.store.rows("filings")
                ) else "state",
            )
        return result

    def data_version(self) -> str:
        """应用当前数据版本：资料指纹 + 接收状态指纹。"""
        return f"{self.store.fingerprint}-{self.state_fingerprint}"

    # ------------------------------------------------------------------ 内部
    def _all_filing_rows(self) -> list[dict[str, Any]]:
        return list(self.store.rows("filings")) + list(self.incoming_filings)

    def _check_filing_references(self, row: dict[str, Any]) -> None:
        enterprises = self.store.by_id("enterprises")
        projects = self.store.by_id("projects")
        if row["enterprise"] not in enterprises:
            raise RevisionError(f"企业不存在：{row['enterprise']}")
        if row["project"] not in projects:
            raise RevisionError(f"项目不存在：{row['project']}")
        date.fromisoformat(row["received_on"])
        if not (isinstance(row["revenue_minor"], int) and row["revenue_minor"] >= 0):
            raise RevisionError("收入必须为非负整数（美分）")
        if not (isinstance(row["hours"], (int, float)) and row["hours"] >= 0):
            raise RevisionError("工时必须为非负数")

    def _validate_incoming_filings(self) -> None:
        for row in self.incoming_filings:
            self._check_filing_references(row)

    def _validate_chain(self) -> None:
        seen: dict[str, int] = {}
        for event in sorted(self.events, key=lambda e: (e.filing, e.version)):
            if event.action not in VALID_ACTIONS:
                raise RevisionError(f"修订动作不合法：{event.id}")
            prev = seen.get(event.filing, 1)
            if event.version <= prev:
                raise RevisionError(
                    f"修订版本必须递增：{event.filing} 版本 {event.version}"
                )
            seen[event.filing] = event.version

    def _append_jsonl(self, name: str, row: dict[str, Any]) -> None:
        path = self.state_dir / name
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _compute_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.state_dir.glob("incoming_*.jsonl")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    def _refresh(self) -> None:
        self._validate_chain()
        self.state_fingerprint = self._compute_fingerprint()
