"""命令行入口：申报接收、可比性分析、成员查询与发布申请审查。

示例：
  python -m benchmark.cli check
  python -m benchmark.cli receive-filing state/new_filing.json
  python -m benchmark.cli receive-revision state/new_revision.json
  python -m benchmark.cli benchmark --request REQ1
  python -m benchmark.cli publish --request REQ2 --today 2026-11-23
  python -m benchmark.cli approve --request REQ3 --today 2026-11-23
  python -m benchmark.cli self-detail --enterprise E00 --requester E00
  python -m benchmark.cli state
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .access import AccessError
from .materials import MaterialError
from .revisions import RevisionError
from .service import BenchmarkService, build_context

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "state"


def _load_request(store, request_id: str) -> dict:
    for row in store.rows("requests"):
        if row["id"] == request_id:
            return dict(row)
    raise SystemExit(f"申请不存在：{request_id}")


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨境服务商对标应用")
    parser.add_argument("--root", default=str(ROOT), help="资料根目录")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="本地状态目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="校验资料指纹与结构")

    p_filing = sub.add_parser("receive-filing", help="接收一份新申报")
    p_filing.add_argument("payload", help="JSON 文件路径")

    p_rev = sub.add_parser("receive-revision", help="接收一条修订/撤回")
    p_rev.add_argument("payload", help="JSON 文件路径")

    p_bench = sub.add_parser("benchmark", help="处理成员对标申请")
    p_bench.add_argument("--request", required=True)
    p_bench.add_argument("--requester", default=None, help="覆盖申请发起方用于权限演示")

    p_pub = sub.add_parser("publish", help="审查联盟发布申请（只审查不写台账）")
    p_pub.add_argument("--request", required=True)
    p_pub.add_argument("--today", default="2026-11-23")

    p_app = sub.add_parser("approve", help="审查并把通过的格写入发布台账")
    p_app.add_argument("--request", required=True)
    p_app.add_argument("--today", default="2026-11-23")

    p_wd = sub.add_parser("withdraw", help="撤回一条已发布格（只追加事件，不消除事实）")
    p_wd.add_argument("--cell", required=True)
    p_wd.add_argument("--today", default="2026-11-23")
    p_wd.add_argument("--reason", default="成员申请撤回")

    p_self = sub.add_parser("self-detail", help="企业读取自身明细")
    p_self.add_argument("--enterprise", required=True)
    p_self.add_argument("--requester", required=True)

    sub.add_parser("state", help="输出版本指纹与台账概况")

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            store = __import__("benchmark.materials", fromlist=["MaterialStore"]).MaterialStore.load(args.root)
            _print({"status": "ok", "fingerprint": store.fingerprint, "tables": {k: len(v) for k, v in store.tables.items()}})
            return 0

        if args.command in {"receive-filing", "receive-revision"}:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
            ctx = build_context(args.root, args.state)
            if args.command == "receive-filing":
                ctx.revisions.receive_filing(payload)
            else:
                ctx.revisions.receive_revision(payload)
            _print({"status": "accepted", "data_version": ctx.revisions.data_version()})
            return 0

        ctx = build_context(args.root, args.state)
        service = BenchmarkService(ctx)

        if args.command == "benchmark":
            request = _load_request(ctx.store, args.request)
            if args.requester:
                request["requester"] = args.requester
            result = service.benchmark(request)
            _print(service.ctx.access.member_view(result, request["requester"]))
        elif args.command == "publish":
            request = _load_request(ctx.store, args.request)
            _print(service.review_publication(request, args.today))
        elif args.command == "approve":
            request = _load_request(ctx.store, args.request)
            review = service.review_publication(request, args.today)
            entries = service.approve(review, args.today)
            _print({"review": review, "published": entries})
        elif args.command == "withdraw":
            event = ctx.ledger.withdraw(args.cell, args.today, args.reason)
            _print(
                {
                    "event": event,
                    "note": "撤回只追加事件；原发布仍作为审查约束，相同信息不得重新释放",
                    "known_cells_kept": len(ctx.ledger.known_cells(include_withdrawn=True)),
                }
            )
        elif args.command == "self-detail":
            _print(service.self_detail(args.enterprise, args.requester))
        elif args.command == "state":
            _print(
                {
                    "data_version": ctx.data_version,
                    "material_fingerprint": ctx.store.fingerprint,
                    "state_fingerprint": ctx.revisions.state_fingerprint,
                    "ledger_fingerprint": ctx.ledger.fingerprint(),
                    "ledger_entries": len(ctx.ledger.entries),
                    "as_of_period": ctx.as_of_period,
                }
            )
        return 0
    except (MaterialError, RevisionError, AccessError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
