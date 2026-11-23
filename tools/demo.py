"""端到端演示：在隔离的临时状态目录中走通申报接收、对标、发布审查与撤回。

用法：python tools/demo.py
该脚本不改动 data/，也不触碰默认 state/，全部状态放在系统临时目录。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.service import BenchmarkService, build_context


def main() -> None:
    state_dir = Path(tempfile.mkdtemp(prefix="benchmark-demo-"))
    try:
        ctx = build_context(str(ROOT), str(state_dir))
        service = BenchmarkService(ctx)
        store = ctx.store

        print("== 1. 资料版本 ==")
        print("data_version:", ctx.data_version)

        print("\n== 2. 接收一条新申报与一条修订 ==")
        filing = json.loads((ROOT / "examples/new_filing.json").read_text(encoding="utf-8"))
        revision = json.loads((ROOT / "examples/new_revision.json").read_text(encoding="utf-8"))
        ctx.revisions.receive_filing(filing)
        ctx.revisions.receive_revision(revision)
        print("新 data_version:", ctx.data_version)

        print("\n== 3. 成员对标 REQ1（EU / publishing+design）==")
        req1 = next(dict(r) for r in store.rows("requests") if r["id"] == "REQ1")
        benchmark = service.benchmark(req1)
        view = ctx.access.member_view(benchmark, req1["requester"])
        for group in view["groups"]:
            print(
                group["metric"],
                "sufficient=", group["sufficient"],
                "peers=", group["peer_count"],
                "dominance=", group["dominance"]["flagged"],
            )
        print("多指标相容:", view["compatibility"]["compatible"])

        print("\n== 4. 发布审查 REQ2（单企业格，历史相减识别）==")
        req2 = next(dict(r) for r in store.rows("requests") if r["id"] == "REQ2")
        review2 = service.review_publication(req2, "2026-11-23")
        for decision in review2["decisions"]:
            print(
                decision["cell_id"],
                decision["disposition"],
                decision["derived_interval_minor"],
                "constraints=", decision["constraints_used"],
            )

        print("\n== 5. 交叉维度申请的同批相减防护 ==")
        cross = json.loads(
            (ROOT / "examples/cross_dimension_request.json").read_text(encoding="utf-8")
        )
        review_cross = service.review_publication(cross, "2026-11-23")
        for decision in review_cross["decisions"]:
            print(decision["cell_id"][-12:], decision["disposition"])

        print("\n== 6. 批准 REQ3 后撤回，约束不消除 ==")
        req3 = next(dict(r) for r in store.rows("requests") if r["id"] == "REQ3")
        review3 = service.review_publication(req3, "2026-11-23")
        entries = service.approve(review3, "2026-11-23")
        print("写入台账:", [(e["id"], e["lower_minor"], e["upper_minor"]) for e in entries])
        if entries:
            ctx.ledger.withdraw(entries[0]["id"], "2026-11-24", "演示撤回")
            again = service.review_publication(req3, "2026-11-25")
            print(
                "撤回后再审约束数:",
                len(again["decisions"][0]["constraints_used"]),
                again["decisions"][0]["constraints_used"],
            )

        print("\n演示完成，临时状态目录：", state_dir)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
