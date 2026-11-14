"""Synthetic candidate fixtures for the U2-B filter matrix (lane B5).

Why this file exists
--------------------
The reason table (B1), the explanation (B2), the re-evaluation (B3) and the plan
snapshot (B4) are built in parallel lanes, so this lane must not import any of
them.  What it publishes instead is plain data: nine candidates, the conditions
each one meets, the verdict the frozen contract requires and the reason codes
that carry that verdict.  The integrator can then assert B2 against these
fixtures without either side having to change for the other.

Where a value is a contract decision it says so; where the contract is silent
the choice is noted in the test file, next to the assertion that pins it.

Shape of one fixture (all keys are required):
    name              stable scenario name (the matrix indexes by it)
    candidate         mapping with exactly illust_id, kind, bookmarks, ai_flag,
                      available - the five fields the explanation reads
    conditions        mapping with at most min_bookmarks and exclude_ai; {}
                      means "no filtering conditions at all"
    expected_verdict  match | exclude | unknown
    expected_codes    tuple of codes from REASON_CODES, in the order the
                      explanation is expected to report them
    expected_fields   tuple of fields parallel to expected_codes; the contract
                      only requires the codes, this field exists so a test can
                      prove that kind=unknown and bookmarks=None both map to
                      field_unknown but on *different* fields

Nothing here touches a database, the network or a real gallery file.
"""
from __future__ import annotations

from typing import Any, Mapping

#: Closed set from the U2-B contract (B1).  A code outside this tuple is a bug.
REASON_CODES = (
    "favorites_below",
    "ai_excluded",
    "type_unsupported",
    "field_unknown",
    "access_denied",
)

#: Closed set of fields a reason may point at.
FIELDS = ("bookmarks", "ai_flag", "type", "availability", "original_urls")

#: The nine scenario names, in the order the contract lists them.
SCENARIO_NAMES = (
    "bookmarks_below",
    "ai_excluded",
    "ugoira",
    "kind_unknown",
    "bookmarks_missing",
    "ai_flag_missing",
    "unavailable",
    "clean_match",
    "no_conditions",
)

_CANDIDATE_KEYS = ("illust_id", "kind", "bookmarks", "ai_flag", "available")
_CONDITION_KEYS = ("min_bookmarks", "exclude_ai")


def _candidate(
    illust_id: str,
    *,
    kind: str,
    bookmarks: Any,
    ai_flag: Any,
    available: Any,
) -> dict[str, Any]:
    """One synthetic candidate; extra keys are refused on purpose."""
    return {
        "illust_id": illust_id,
        "kind": kind,
        "bookmarks": bookmarks,
        "ai_flag": ai_flag,
        "available": available,
    }


def _fixture(
    name: str,
    *,
    candidate: Mapping[str, Any],
    conditions: Mapping[str, Any],
    verdict: str,
    codes: tuple[str, ...],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "candidate": dict(candidate),
        "conditions": dict(conditions),
        "expected_verdict": verdict,
        "expected_codes": tuple(codes),
        "expected_fields": tuple(fields),
    }


_SCENARIOS = {
    # 收藏数低于阈值：已知拒绝，不是未知。
    "bookmarks_below": _fixture(
        "bookmarks_below",
        candidate=_candidate("1001", kind="illust", bookmarks=10, ai_flag=False, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": False},
        verdict="exclude",
        codes=("favorites_below",),
        fields=("bookmarks",),
    ),
    # AI 命中且要求排除 AI。
    "ai_excluded": _fixture(
        "ai_excluded",
        candidate=_candidate("1002", kind="illust", bookmarks=500, ai_flag=True, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": True},
        verdict="exclude",
        codes=("ai_excluded",),
        fields=("ai_flag",),
    ),
    # 动图：类型不支持（不是字段未知）。
    "ugoira": _fixture(
        "ugoira",
        candidate=_candidate("1003", kind="ugoira", bookmarks=500, ai_flag=False, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": False},
        verdict="exclude",
        codes=("type_unsupported",),
        fields=("type",),
    ),
    # 类型未知：字段未知，指 type。
    "kind_unknown": _fixture(
        "kind_unknown",
        candidate=_candidate("1004", kind="unknown", bookmarks=500, ai_flag=False, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": False},
        verdict="unknown",
        codes=("field_unknown",),
        fields=("type",),
    ),
    # 收藏数缺失：字段未知，指 bookmarks —— 与上一条同为 field_unknown 但字段不同。
    "bookmarks_missing": _fixture(
        "bookmarks_missing",
        candidate=_candidate("1005", kind="illust", bookmarks=None, ai_flag=False, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": False},
        verdict="unknown",
        codes=("field_unknown",),
        fields=("bookmarks",),
    ),
    # AI 标记缺失，且条件要求排除 AI：字段未知，指 ai_flag。
    "ai_flag_missing": _fixture(
        "ai_flag_missing",
        candidate=_candidate("1006", kind="illust", bookmarks=500, ai_flag=None, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": True},
        verdict="unknown",
        codes=("field_unknown",),
        fields=("ai_flag",),
    ),
    # 明确不可访问（available is False）：已知拒绝。
    "unavailable": _fixture(
        "unavailable",
        candidate=_candidate("1007", kind="illust", bookmarks=500, ai_flag=False, available=False),
        conditions={"min_bookmarks": 100, "exclude_ai": False},
        verdict="exclude",
        codes=("access_denied",),
        fields=("availability",),
    ),
    # 全字段齐且满足全部条件：通过，且不得携带任何原因码。
    "clean_match": _fixture(
        "clean_match",
        candidate=_candidate("1008", kind="illust", bookmarks=500, ai_flag=False, available=True),
        conditions={"min_bookmarks": 100, "exclude_ai": True},
        verdict="match",
        codes=(),
        fields=(),
    ),
    # 没有任何条件：即使候选本身"很差"（0 收藏、AI），也不该被排除。
    "no_conditions": _fixture(
        "no_conditions",
        candidate=_candidate("1009", kind="manga", bookmarks=0, ai_flag=True, available=True),
        conditions={},
        verdict="match",
        codes=(),
        fields=(),
    ),
}

#: name -> fixture, in the contract's order.
SCENARIOS: dict[str, dict[str, Any]] = {name: _SCENARIOS[name] for name in SCENARIO_NAMES}

#: The same nine fixtures as a list, for loops in the matrix.
ALL_SCENARIOS: list[dict[str, Any]] = [SCENARIOS[name] for name in SCENARIO_NAMES]


def scenario(name: str) -> dict[str, Any]:
    """One fixture by name; an unknown name is an error, never a silent miss."""
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError("unknown scenario: %r" % (name,)) from None


def codes_of(scenarios: Any = None) -> list[str]:
    """Every expected code across the scenarios, in order (duplicates kept)."""
    source = ALL_SCENARIOS if scenarios is None else scenarios
    codes: list[str] = []
    for item in source:
        codes.extend(item["expected_codes"])
    return codes


def sum_codes(scenarios: Any = None) -> dict[str, int]:
    """How often each code is expected; codes that never appear are absent."""
    counts: dict[str, int] = {}
    for code in codes_of(scenarios):
        counts[code] = counts.get(code, 0) + 1
    return counts
