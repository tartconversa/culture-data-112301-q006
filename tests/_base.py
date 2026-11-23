"""测试公共夹具：在临时目录中构建干净的应用上下文。"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from benchmark.service import BenchmarkService, build_context

ROOT = Path(__file__).resolve().parents[1]


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = Path(tempfile.mkdtemp(prefix="bench-state-"))
        self.ctx = build_context(str(ROOT), str(self.state_dir))
        self.service = BenchmarkService(self.ctx)

    def tearDown(self) -> None:
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def request(self, request_id: str) -> dict:
        for row in self.ctx.store.rows("requests"):
            if row["id"] == request_id:
                return dict(row)
        raise KeyError(request_id)
