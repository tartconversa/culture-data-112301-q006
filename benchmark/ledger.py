"""联盟发布台账（只追加）。

- 已发布内容写入 state/publication_ledger.jsonl，任何修订或撤回都不能删除或改写；
- 撤回只追加一条 withdrawn 事件，审查引擎仍把原始发布视为有效约束，因此同一条
  信息不能借撤回“重新释放”；
- 每条发布绑定数据版本与口径键；审批时若当前数据版本与申请/发布时不同，
  返回版本漂移说明与影响范围，历史发布保留原依据不变。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .disclosure import KnownCell


class LedgerError(ValueError):
    """台账操作不合法。"""


@dataclass
class PublicationLedger:
    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def open(cls, state_dir: str | Path, seed_cells: list[dict[str, Any]] | None = None) -> "PublicationLedger":
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "publication_ledger.jsonl"
        ledger = cls(path=path)
        if not path.exists() and seed_cells:
            for row in seed_cells:
                ledger.entries.append(
                    {
                        "event": "published",
                        "id": row["id"],
                        "published_on": row["published_on"],
                        "members": list(row["members"]),
                        "metric": row["metric"],
                        "period": row["period"],
                        "lower_minor": int(row["lower_minor"]),
                        "upper_minor": int(row["upper_minor"]),
                        "audience": row["audience"],
                        "caliber_key": row.get("caliber_key", "all-services/external-net"),
                        "services": list(
                            row.get(
                                "services",
                                ["publishing", "localization", "design", "support"],
                            )
                        ),
                        "data_version": row.get("data_version", "material-seed"),
                        "supersedes": None,
                        "withdrawn": False,
                    }
                )
            ledger._flush()
        elif path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ledger.entries.append(json.loads(line))
        return ledger

    def publish(
        self,
        cell_id: str,
        members: list[str],
        metric: str,
        period: str,
        lower_minor: int,
        upper_minor: int,
        audience: str,
        caliber_key: str,
        services: list[str],
        data_version: str,
        published_on: str,
    ) -> dict[str, Any]:
        date.fromisoformat(published_on)
        if any(e["id"] == cell_id and e["event"] == "published" for e in self.entries):
            raise LedgerError(f"公开格标识已存在：{cell_id}")
        if upper_minor < lower_minor:
            raise LedgerError("公开区间上界不得小于下界")
        entry = {
            "event": "published",
            "id": cell_id,
            "published_on": published_on,
            "members": sorted(members),
            "metric": metric,
            "period": period,
            "lower_minor": int(lower_minor),
            "upper_minor": int(upper_minor),
            "audience": audience,
            "caliber_key": caliber_key,
            "services": sorted(services),
            "data_version": data_version,
            "supersedes": None,
            "withdrawn": False,
        }
        self.entries.append(entry)
        self._flush()
        return dict(entry)

    def withdraw(self, cell_id: str, received_on: str, reason: str) -> dict[str, Any]:
        """撤回只追加事件；原发布仍作为历史约束参与审查。"""
        if not any(
            e["id"] == cell_id and e["event"] == "published" and not e["withdrawn"]
            for e in self.entries
        ):
            raise LedgerError(f"不存在可撤回的有效发布：{cell_id}")
        event = {
            "event": "withdrawn",
            "id": cell_id,
            "received_on": received_on,
            "reason": reason,
        }
        self.entries.append(event)
        for entry in self.entries:
            if entry["id"] == cell_id and entry["event"] == "published":
                entry["withdrawn"] = True
        self._flush()
        return event

    def known_cells(self, include_withdrawn: bool = True) -> list[KnownCell]:
        """审查约束集合。

        include_withdrawn=True（默认）时，即使发布被撤回，其事实仍保留约束——
        相同接收者已经见过的结果不能通过撤回消除。
        """
        cells: list[KnownCell] = []
        for entry in self.entries:
            if entry["event"] != "published":
                continue
            if entry["withdrawn"] and not include_withdrawn:
                continue
            cells.append(
                KnownCell(
                    id=entry["id"],
                    members=frozenset(entry["members"]),
                    metric=entry["metric"],
                    period=entry["period"],
                    lower_minor=entry["lower_minor"],
                    upper_minor=entry["upper_minor"],
                    audience=entry["audience"],
                    caliber_key=entry["caliber_key"],
                    services=frozenset(entry.get("services", [])),
                    historical=True,
                )
            )
        return cells

    def check_version_drift(
        self, current_version: str, request_version: str | None
    ) -> dict[str, Any]:
        if request_version is None:
            return {"drift": False, "note": "申请未绑定资料版本，按当前版本审查"}
        if request_version == current_version:
            return {"drift": False, "note": "申请版本与当前资料版本一致"}
        affected = sorted(
            {
                entry["id"]
                for entry in self.entries
                if entry["event"] == "published" and entry["data_version"] != current_version
            }
        )
        return {
            "drift": True,
            "request_version": request_version,
            "current_version": current_version,
            "affected_historical_cells": affected,
            "note": (
                "申请提交后资料版本发生变化；历史发布保留原依据，本次结论按新版本重算，"
                f"受影响的历史公开格：{affected or '无'}"
            ),
        }

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for entry in self.entries:
            digest.update(
                json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
        return digest.hexdigest()[:16]

    def _flush(self) -> None:
        with self.path.open("w", encoding="utf-8") as stream:
            for entry in self.entries:
                stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
