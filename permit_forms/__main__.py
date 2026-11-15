import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _rehearsal(args):
    from .drill import Rehearsal
    from .ledger import Ledger

    path = Path(args.snapshot) if args.snapshot else None
    return Rehearsal.load(Path(args.scenario) if args.scenario else None, Ledger(path))


def _review(args):
    from .drill import Rehearsal
    return _rehearsal(args).review(args.application)


def _plan(args):
    rehearsal = _rehearsal(args)
    plan = rehearsal.plan(args.application)
    if args.confirm:
        from .plan import confirm_plan

        plan = confirm_plan(plan, args.confirm)
    return plan.as_dict()


def _preflight(args):
    from .drill import Rehearsal, preflight_report
    from .plan import confirm_plan

    rehearsal = _rehearsal(args)
    plan = rehearsal.plan(args.application)
    if args.confirm:
        plan = confirm_plan(plan, args.confirm)
    return preflight_report(rehearsal, plan, args.current_form)


def _impact(args):
    return _rehearsal(args).impact(args.before, args.after)


def _ledger(args):
    from .drill import Rehearsal

    rehearsal = _rehearsal(args)
    if args.explain:
        return rehearsal.ledger.explain(args.explain)
    return {
        "rules": [item.as_dict() for item in rehearsal.ledger.rules()],
        "all_rule_versions": [item.as_dict() for item in rehearsal.ledger.all_rule_versions()],
        "case_decisions": [item.as_dict() for item in rehearsal.ledger.case_decisions()],
        "events": [item.as_dict() for item in rehearsal.ledger.events()],
    }


def _drill(args):
    """一条命令跑完映射审阅 → 计划确认 → 版本变化衔接，全程不提交。"""
    from .drill import run_rehearsal

    return run_rehearsal(
        args.application[0] if args.application else "APP-WEST-001",
        Path(args.scenario) if args.scenario else None,
        actor=args.actor,
        after_key=args.after,
    )


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、归档与本地映射演练")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)


    def rehearsal_arguments(sub):
        sub.add_argument("--scenario", type=Path, help="合成演练资料，默认打包资料")
        sub.add_argument("--snapshot", type=Path, help="可选的裁定记录 SQLite 快照")

    review = subcommands.add_parser("review", help="按申请输出可解释的材料映射")
    review.add_argument("--application", required=True)
    rehearsal_arguments(review)

    plan = subcommands.add_parser("plan", help="生成填报计划，可选确认")
    plan.add_argument("--application", required=True)
    plan.add_argument("--confirm")
    rehearsal_arguments(plan)

    preflight_command = subcommands.add_parser("preflight", help="检查计划在当前表单下能否执行")
    preflight_command.add_argument("--application", required=True)
    preflight_command.add_argument("--current-form", required=True)
    preflight_command.add_argument("--confirm")
    rehearsal_arguments(preflight_command)

    impact = subcommands.add_parser("impact", help="比较两个表单版本并标出受影响项")
    impact.add_argument("--before", required=True)
    impact.add_argument("--after", required=True)
    rehearsal_arguments(impact)

    ledger = subcommands.add_parser("ledger", help="查看规则、例外与裁定历史")
    ledger.add_argument("--explain")
    rehearsal_arguments(ledger)

    drill = subcommands.add_parser("drill", help="本地演练映射审阅、计划确认与版本变化")
    drill.add_argument("--application", action="append", help="要确认计划的申请，默认取第一笔")
    drill.add_argument("--after", help="用于衔接检查的目标表单版本，例如 west|lab|2026-c")
    drill.add_argument("--actor", default="申报员-演练")
    rehearsal_arguments(drill)

    args = parser.parse_args()
    handlers = {
        "review": _review,
        "plan": _plan,
        "preflight": _preflight,
        "impact": _impact,
        "ledger": _ledger,
        "drill": _drill,
    }
    if args.command in handlers:
        _print(handlers[args.command](args))
        return
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path), ensure_ascii=False, indent=2))
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
