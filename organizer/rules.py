"""A versioned, literal, offline ruleset that turns evidence into group names.

What this module is not: it does not look at images, does not call a model and
does not judge whether a rule is *good*.  It applies one frozen rule set to
evidence that a human or an AI already collected (the tags of the works that
were actually viewed), and it keeps apart three answers that must never be
collapsed:

* the evidence does not support any group -> confidence=unknown, needs_more
* no rule matched                         -> confidence=unknown, needs_more
* matched, but only weakly                -> confidence=low,      needs_more

None of them is "this artist needs no groups".  That answer would have to come
from a rule set that says so explicitly, and no such rule exists.

Matching is literal on purpose: a rule matches when a tag equals the rule text
or contains it, compared casefolded.  There is no regex and no fuzzy matching,
because a ruleset that can be silently re-interpreted is not a ruleset that can
be fingerprinted and audited.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping

from organizer.common import OrganizerError, groups as group_names, integer, text

DIMENSIONS = ("topic", "style", "usage")
CONFIDENCE = ("high", "low", "unknown")

#: The web front end lets one artist belong to at most five groups.
MAX_GROUPS = 5

#: Rule text longer than this is refused instead of silently truncated.
MAX_MATCH = 64

#: How a rule text is compared with a tag.  "equals" is the default because
#: containment produces false positives that end up as wrong group names:
#: a rule for "cat" would otherwise claim an artist whose tag is
#: "concatenate".  Authors who really want substring matching ask for it.
MATCH_KINDS = ("equals", "contains")

#: Why a classification came out the way it did.  Closed on purpose.
REASONS = (
    "evidence_missing",
    "no_rule_matched",
    "groups_truncated",
    "low_confidence",
    "single_weak_hit",
)


class RuleError(OrganizerError):
    """A rule or rule set that cannot be used, with the offending rule named."""

    def __init__(self, code: str, rule_id: str = "") -> None:
        self.rule_id = rule_id
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Rule:
    """One literal rule.  match is text, never a pattern."""

    rule_id: str
    dimension: str
    match: str
    suggest_groups: tuple[str, ...]
    weight: int = 1
    match_kind: str = "equals"

    def __post_init__(self) -> None:
        rule_id = text(self.rule_id, 64)
        if self.dimension not in DIMENSIONS:
            raise RuleError("rule_dimension_invalid", rule_id)
        match = text(self.match, MAX_MATCH)
        if self.match_kind not in MATCH_KINDS:
            raise RuleError("rule_match_kind_invalid", rule_id)
        try:
            suggested = tuple(group_names(list(self.suggest_groups)))
        except OrganizerError as error:
            raise RuleError(error.code, rule_id) from None
        if not suggested:
            raise RuleError("rule_groups_required", rule_id)
        weight = integer(self.weight, 1, 100)
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "match", match)
        object.__setattr__(self, "suggest_groups", suggested)
        object.__setattr__(self, "weight", weight)

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "dimension": self.dimension,
            "match": self.match,
            "suggest_groups": list(self.suggest_groups),
            "weight": self.weight,
            "match_kind": self.match_kind,
        }


@dataclass(frozen=True, slots=True)
class RuleSet:
    """An immutable, versioned rule set.  The version is part of the identity."""

    version: str
    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        version = text(self.version, 100)
        rules = tuple(self.rules)
        seen: set[str] = set()
        for rule in rules:
            if not isinstance(rule, Rule):
                raise RuleError("rule_not_a_rule")
            if rule.rule_id in seen:
                raise RuleError("rule_id_duplicate", rule.rule_id)
            seen.add(rule.rule_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "rules", rules)


def ruleset_fingerprint(ruleset: RuleSet) -> str:
    """sha256 over the canonical form: rule order does not matter, version does."""
    if not isinstance(ruleset, RuleSet):
        raise RuleError("ruleset_required")
    payload = {
        "version": ruleset.version,
        "rules": [rule.as_dict() for rule in sorted(ruleset.rules, key=lambda r: r.rule_id)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tags(evidence: object) -> list[str]:
    """The evidence this module can use: a non-empty list of tag strings."""
    if not isinstance(evidence, Mapping):
        raise OrganizerError("evidence_not_a_mapping")
    raw = evidence.get("tags")
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    tags = []
    for item in raw:
        if not isinstance(item, str):
            raise OrganizerError("evidence_tag_invalid")
        cleaned = item.strip()
        if cleaned:
            tags.append(cleaned)
    return tags


def _matched(rule: Rule, folded: list[tuple[str, str]]) -> list[str]:
    """Casefolded comparison only: no regex, no fuzzy match, no prefix match."""
    needle = rule.match.casefold()
    if rule.match_kind == "equals":
        return [original for folded_tag, original in folded if folded_tag == needle]
    return [original for folded_tag, original in folded if needle in folded_tag]


def classify(evidence: object, ruleset: RuleSet) -> dict:
    """Apply the rule set to already-collected evidence.  Reads only."""
    if not isinstance(ruleset, RuleSet):
        raise RuleError("ruleset_required")
    tags = _tags(evidence)
    if not tags:
        return {
            "groups": [],
            "anchors": [],
            "confidence": "unknown",
            "needs_more": True,
            "reasons": ["evidence_missing"],
        }
    folded = [(tag.casefold(), tag) for tag in tags]
    weights: dict[str, int] = {}
    anchors: list[dict] = []
    for rule in sorted(ruleset.rules, key=lambda r: r.rule_id):
        hits = _matched(rule, folded)
        if not hits:
            continue
        for name in rule.suggest_groups:
            weights[name] = weights.get(name, 0) + rule.weight
        anchors.append(
            {
                "rule_id": rule.rule_id,
                "dimension": rule.dimension,
                "match": rule.match,
                "match_kind": rule.match_kind,
                "tags": sorted(hits),
                "weight": rule.weight,
            }
        )
    if not anchors:
        return {
            "groups": [],
            "anchors": [],
            "confidence": "unknown",
            "needs_more": True,
            "reasons": ["no_rule_matched"],
        }
    ordered = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    reasons: list[str] = []
    if len(ordered) > MAX_GROUPS:
        ordered = ordered[:MAX_GROUPS]
        reasons.append("groups_truncated")
    total_weight = sum(weights.values())
    if total_weight >= 2:
        confidence = "high"
    else:
        confidence = "low"
        reasons.append("single_weak_hit")
    if confidence != "high":
        reasons.append("low_confidence")
    return {
        "groups": [name for name, _weight in ordered],
        "anchors": anchors,
        "confidence": confidence,
        "needs_more": confidence != "high",
        "reasons": reasons,
    }


def changed_by_version(
    per_artist: Mapping[str, object], old: RuleSet, new: RuleSet
) -> dict:
    """Which artists the move old -> new would change.  A listing, never a write."""
    if not isinstance(per_artist, Mapping):
        raise OrganizerError("evidence_not_a_mapping")
    changed: dict[str, dict] = {}
    for artist_id in sorted(per_artist):
        before = classify(per_artist[artist_id], old)
        after = classify(per_artist[artist_id], new)
        if before["groups"] != after["groups"] or before["confidence"] != after["confidence"]:
            changed[str(artist_id)] = {"before": before, "after": after}
    return changed
