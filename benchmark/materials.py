"""资料加载与指纹校验。

所有分析必须从通过指纹校验的资料构建，避免在损坏或被篡改的资料上发布结论。
资料版本指纹覆盖 data 目录内全部登记文件；应用状态（修订、发布台账）的版本
由各自模块在数据版本之上另行追加。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MaterialError(ValueError):
    """资料不可信或结构不符时抛出。"""


@dataclass(frozen=True)
class MaterialStore:
    root: Path
    tables: dict[str, list[dict[str, Any]]]
    catalog: dict[str, Any]
    fingerprint: str

    @classmethod
    def load(cls, root: str | Path) -> "MaterialStore":
        root = Path(root)
        catalog_path = root / "data" / "catalog.json"
        if not catalog_path.exists():
            raise MaterialError(f"资料目录缺少 catalog.json：{catalog_path}")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        tables: dict[str, list[dict[str, Any]]] = {}
        digest = hashlib.sha256()
        for name, spec in catalog["tables"].items():
            path = root / "data" / f"{name}.jsonl"
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise MaterialError(f"资料指纹不符 {name}")
            rows = [
                json.loads(line)
                for line in raw.decode("utf-8").splitlines()
                if line.strip()
            ]
            if len(rows) != spec["row_count"]:
                raise MaterialError(f"资料行数不符 {name}")
            identifiers = [row[spec["key"]] for row in rows]
            if len(identifiers) != len(set(identifiers)):
                raise MaterialError(f"资料主键重复 {name}")
            for row in rows:
                if set(row) != set(spec["fields"]):
                    raise MaterialError(f"字段不符 {name}")
                for field, definition in spec["fields"].items():
                    value = row[field]
                    if value is None and definition["nullable"]:
                        continue
                    if type(value).__name__ != definition["type"]:
                        raise MaterialError(f"字段类型不符 {name}.{field}")
            tables[name] = rows
            digest.update(name.encode("utf-8"))
            digest.update(raw)
        store = cls(
            root=root,
            tables=tables,
            catalog=catalog,
            fingerprint=digest.hexdigest()[:16],
        )
        store._check_references()
        return store

    def _check_references(self) -> None:
        for name, spec in self.catalog["tables"].items():
            for field, (target, key) in spec.get("references", {}).items():
                keys = {row[key] for row in self.tables[target]}
                for row in self.tables[name]:
                    if row[field] is not None and row[field] not in keys:
                        raise MaterialError(f"直接引用不存在 {name}.{field}")

    def rows(self, name: str) -> list[dict[str, Any]]:
        return self.tables[name]

    def by_id(self, name: str) -> dict[str, dict[str, Any]]:
        key = self.catalog["tables"][name]["key"]
        return {row[key]: row for row in self.tables[name]}
