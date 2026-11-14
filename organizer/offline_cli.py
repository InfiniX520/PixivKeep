"""Three offline commands: plan a batch, plan a group scope, assemble a report.

Nothing here opens a connection or writes a file.  The inputs are local JSON
(an exported snapshot, a pool list, a batch/decisions/scope dump) and the
output goes to stdout.  The live actions stay in cli.py: no command in this
module can reach the website, and none of them takes an --execute flag.

Exit codes follow the wiring plan: 0 when the input was complete and the
conclusion holds, 3 when an input cannot be read or is refused, 5 when the
answer depends on evidence the input does not carry (a scope whose membership
is only partially known, a report whose coverage cannot be claimed).  A number
that was not measured is printed as 未知 - never as 0.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from organizer.common import OrganizerError
from organizer.batches import BatchError, plan_batch
from organizer.group_scope import AmbiguousGroupError, build_scope

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_UNPROVEN = 5

OFFLINE_COMMANDS = ("batch-plan", "scope-plan", "batch-report")

_VISIBILITY = ("show", "hide")


class OfflineRefusal(OrganizerError):
    """An input that cannot answer; always exit 3, never a traceback."""

    def __init__(self, code, detail="", exit_code=EXIT_REFUSED):
        super().__init__(code)
        # OrganizerError washes any code that is not a plain identifier, and a
        # refusal code carries a subject (input_not_json:pool).  Keep our own so
        # the message names what was refused instead of "organizer_error".
        self.code = str(code)
        self.detail = str(detail)
        self.exit_code = int(exit_code)

    def __str__(self):
        return self.code if not self.detail else "%s: %s" % (self.code, self.detail)


def _load(path, what):
    """Read one JSON input, refusing with a stable code when it cannot answer."""
    target = Path(str(path))
    if not target.is_file():
        raise OfflineRefusal("input_missing:" + what, target)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OfflineRefusal("input_not_json:" + what, "%s (%s)" % (target, error)) from None


def _pool(payload):
    """A pool is either a bare list of ids or {"artists": [...]}."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("artists"), list):
        rows = payload["artists"]
    else:
        raise OfflineRefusal("pool_shape_invalid", "a list of artist ids is required")
    if not rows:
        raise OfflineRefusal("pool_empty", "an empty pool cannot be planned")
    for row in rows:
        if isinstance(row, dict):
            if not row.get("artist_id"):
                raise OfflineRefusal("pool_artist_invalid", row)
        elif not isinstance(row, str) or not row.strip():
            raise OfflineRefusal("pool_artist_invalid", row)
    return rows


def _group_arg(value):
    """show:NAME / hide:NAME becomes a pair; a bare name is left for the engine."""
    text = str(value or "").strip()
    if not text:
        raise OfflineRefusal("group_argument_empty", value)
    head, sep, tail = text.partition(":")
    if sep and head in _VISIBILITY:
        if not tail.strip():
            raise OfflineRefusal("group_argument_empty", value)
        return (head, tail.strip())
    return text


def _positive(value, what, *, allow_zero=True):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise OfflineRefusal("limit_invalid:" + what, value)
    return value


def _value(obj, name):
    """Read a field that may be a property or a zero-argument method."""
    value = getattr(obj, name, None)
    return value() if callable(value) else value


def _json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise OfflineRefusal("result_not_serialisable", error) from None
    return value


def cmd_batch_plan(args):
    payload = _pool(_load(args.pool, "pool"))
    artists = _positive(args.max_artists, "max_artists", allow_zero=False)
    requests = _positive(args.max_requests, "max_requests")
    bytes_budget = _positive(args.max_bytes, "max_bytes")
    seconds = args.max_seconds
    if type(seconds) not in (int, float) or seconds < 0:
        raise OfflineRefusal("limit_invalid:max_seconds", seconds)
    try:
        batch = plan_batch(
            payload,
            account_id="offline",
            artists=artists,
            requests=requests,
            bytes_budget=bytes_budget,
            seconds=float(seconds),
        )
    except BatchError as error:
        raise OfflineRefusal("batch_rejected", error) from None
    data = _json_safe(batch.as_dict())
    lines = [
        "批次: %s" % (batch.batch_id,),
        "画师: %d 位" % (len(batch.steps),),
        "状态: %s" % (batch.status,),
        "停止原因: %s" % (batch.stop_reason or "none",),
        "指纹: %s" % (data.get("fingerprint") or _value(batch, "fingerprint"),),
    ]
    return {"command": "batch-plan", "batch": data}, lines, EXIT_OK


def cmd_scope_plan(args):
    snapshot = _load(args.snapshot, "snapshot")
    if not isinstance(snapshot, dict):
        raise OfflineRefusal("snapshot_shape_invalid", type(snapshot).__name__)
    groups = [_group_arg(value) for value in (args.group or ())]
    if not groups:
        raise OfflineRefusal("group_argument_empty", "--group 至少给一个")
    try:
        scope = build_scope(snapshot, groups=groups)
    except AmbiguousGroupError as error:
        raise OfflineRefusal(getattr(error, "code", "group_ambiguous"), error) from None
    except OrganizerError as error:
        raise OfflineRefusal(getattr(error, "code", "scope_refused"), error) from None
    data = _json_safe(scope.as_dict())
    members = len(scope.artist_ids)
    completeness = scope.completeness
    if completeness == "complete":
        lines = [
            "组: %s" % (", ".join(scope.groups),),
            "画师: %d 位" % members,
            "完整性: 完整",
            "指纹: %s" % (scope.fingerprint,),
        ]
        return {"command": "scope-plan", "scope": data}, lines, EXIT_OK
    lines = [
        "组: %s" % (", ".join(scope.groups),),
        "画师: %d 位" % members,
        "完整性: %s" % ("部分" if completeness == "partial" else "未知"),
        "原因: %s" % (scope.unknown_reason or "snapshot_incomplete",),
        "指纹: %s" % (scope.fingerprint,),
        "注意: 这份范围不能当作该组的全集",
    ]
    return {"command": "scope-plan", "scope": data}, lines, EXIT_UNPROVEN


def _report_engine():
    try:
        module = importlib.import_module("organizer.report")
    except ImportError:
        return None
    return getattr(module, "build_report", None)


def cmd_batch_report(args):
    batch = _load(args.batch, "batch")
    decisions = _load(args.decisions, "decisions")
    scope = _load(args.scope, "scope")
    if not isinstance(batch, dict) or not isinstance(scope, dict):
        raise OfflineRefusal("report_input_shape_invalid", "batch and scope must be objects")
    if not isinstance(decisions, list):
        raise OfflineRefusal("report_input_shape_invalid", "decisions must be a list")
    builder = _report_engine()
    if builder is None:
        # The report engine is a separate work package.  Refusing beats printing a
        # summary assembled here: two implementations would drift apart.
        raise OfflineRefusal(
            "report_engine_missing",
            "organizer.report 尚未合流；这批命令只组装，不自己算报告",
        )
    try:
        report = builder(batch, decisions, scope, measured=None, generated_at="")
    except TypeError as error:
        raise OfflineRefusal("report_engine_contract", error) from None
    except OrganizerError as error:
        raise OfflineRefusal(getattr(error, "code", "report_refused"), error) from None
    data = _json_safe(report)
    lines = _report_lines(data)
    unproven = scope.get("completeness") != "complete" or bool(data.get("unknowns"))
    return {"command": "batch-report", "report": data}, lines, (
        EXIT_UNPROVEN if unproven else EXIT_OK
    )


def _report_lines(data):
    lines = []
    counts = data.get("counts")
    if isinstance(counts, dict) and counts:
        parts = ["%s=%s" % (key, counts[key]) for key in sorted(counts)]
        lines.append("状态计数: %s" % (" ".join(parts),))
    else:
        lines.append("状态计数: 未知（报告未提供）")
    unknowns = data.get("unknowns")
    if unknowns:
        lines.append("待人工确认: %d 项" % (len(unknowns),))
        for item in list(unknowns)[:10]:
            if isinstance(item, dict):
                lines.append(
                    "  - %s %s"
                    % (item.get("artist_id", "?"), item.get("reason") or item.get("kind") or "")
                )
            else:
                lines.append("  - %s" % (item,))
    else:
        lines.append("待人工确认: 0 项")
    measured = data.get("planned_vs_measured")
    if isinstance(measured, dict) and measured.get("state") == "unknown":
        lines.append("计划对实测: 未知（%s）" % (measured.get("reason") or "no_measurement",))
    elif isinstance(measured, dict) and measured:
        parts = ["%s=%s" % (key, measured[key]) for key in sorted(measured)]
        lines.append("计划对实测: %s" % (" ".join(parts),))
    else:
        lines.append("计划对实测: 未知（报告未提供）")
    provenance = data.get("provenance")
    if isinstance(provenance, dict) and provenance.get("batch_fingerprint"):
        lines.append("批次指纹: %s" % (provenance["batch_fingerprint"],))
    completeness = (data.get("provenance") or {}).get("scope_completeness")
    if completeness:
        lines.append("范围完整性: %s" % (completeness,))
    return lines


def register(commands):
    """Attach the three offline subcommands to an existing argparse subparsers."""
    batch_plan = commands.add_parser(
        "batch-plan", help="按四个上限把画师池排成一个批次，不联网"
    )
    batch_plan.add_argument("--pool", type=Path, required=True)
    batch_plan.add_argument("--max-artists", type=int, required=True)
    batch_plan.add_argument("--max-requests", type=int, required=True)
    batch_plan.add_argument("--max-bytes", type=int, required=True)
    batch_plan.add_argument("--max-seconds", type=float, required=True)
    scope_plan = commands.add_parser(
        "scope-plan", help="从一份快照算出某个组的下载范围，不联网"
    )
    scope_plan.add_argument("--snapshot", type=Path, required=True)
    scope_plan.add_argument("--group", action="append", default=[])
    batch_report = commands.add_parser(
        "batch-report", help="把批次、冲突决策与范围组装成报告，不联网"
    )
    batch_report.add_argument("--batch", type=Path, required=True)
    batch_report.add_argument("--decisions", type=Path, required=True)
    batch_report.add_argument("--scope", type=Path, required=True)
    for parser in (batch_plan, scope_plan, batch_report):
        parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    return commands


def run(args):
    """Run one offline command and return its exit code (never raises)."""
    handlers = {
        "batch-plan": cmd_batch_plan,
        "scope-plan": cmd_scope_plan,
        "batch-report": cmd_batch_report,
    }
    as_json = bool(getattr(args, "json", False))
    try:
        payload, lines, code = handlers[args.command](args)
    except OfflineRefusal as error:
        if as_json:
            print(json.dumps({"ok": False, "code": error.code, "detail": error.detail},
                             ensure_ascii=False, indent=2))
        else:
            print("被拒: %s" % (error,), file=sys.stderr)
        return error.exit_code
    except OrganizerError as error:
        if as_json:
            print(json.dumps({"ok": False, "code": getattr(error, "code", "organizer_error")},
                             ensure_ascii=False, indent=2))
        else:
            print("被拒: %s" % (getattr(error, "code", error),), file=sys.stderr)
        return EXIT_REFUSED
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for line in lines:
            print(line)
    return code

