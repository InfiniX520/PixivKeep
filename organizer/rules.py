"""A versioned, literal, offline ruleset that turns evidence into group names.

What this module is not: it does not look at images, does not call a model and
does not judge whether a rule is *good*.  It applies one frozen rule set to
evidence that a human or an AI already collected (the tags of the works that
were actually viewed), and it keeps apart three answers that must never be
collapsed:

* the evidence does not support any group -> confidence=unknown, needs_more
* no rule matched                         -> confidence=unknown, needs_more
* matched, but only weakly                -> confidence=low,      needs_more
* matched strongly, but nobody looked     -> confidence=low,      needs_more

The last line is the EA-07 correction (docs/optimization/2026-11-15/
S0_CONTRACTS_R2.md section 7): a tag score is a *match* score, not a look at
the pictures.  A rule weight of 2 used to be enough to answer "high, nothing
more needed", which turned "the tag says cat" into "somebody reviewed this
artist's works".  The two facts are now carried separately:
match_strength says how well the rules matched, visual_evidence says whether a
stored review receipt proves the works were actually viewed, and confidence is
high only when both are.  A plain dict can never prove the second one: the only
constructor that can answer "sufficient" is review_evidence(), which reads the
canonical review document under the receipt's id, requires the caller's copy to
agree with it field by field, and checks the sample it points at.  The fact then
carries the fingerprint of the rule set the review was taken against, and a
consumer refuses to lift a rule set it was not taken against.

None of them is "this artist needs no groups".  That answer would have to come
from a rule set that says so explicitly, and no such rule exists.

Matching is literal on purpose: a rule matches when a tag equals the rule text
or contains it, compared casefolded.  There is no regex and no fuzzy matching,
because a ruleset that can be silently re-interpreted is not a ruleset that can
be fingerprinted and audited.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    # Why a strong tag match still cannot answer "nothing more needed".
    "visual_evidence_missing",
    "visual_evidence_unverified",
    "visual_evidence_insufficient",
    "visual_evidence_stale",
)

#: The four answers visual evidence may give.  "unknown" is not "insufficient":
#: one says nobody showed a receipt, the other says a receipt exists and does
#: not prove enough.  Collapsing them would let a missing observation read as a
#: measured shortfall.
VISUAL_STATES = ("sufficient", "insufficient", "unknown", "stale")

#: The review gate's rule, reused here instead of restated: a receipt may claim
#: completeness only after min(MAX_REQUIRED_WORKS, available_works) *distinct*
#: works were viewed (organizer/samples.py review()).
MAX_REQUIRED_WORKS = 12

#: The exact attestation string samples.review() writes.  A receipt without it
#: was not produced by the gate that asks a human to state they looked.
REVIEW_ATTESTATION = "explicit_observer_statement_not_automatic_image_understanding"


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


@dataclass(frozen=True, slots=True)
class VisualEvidence:
    """Whether anybody actually looked at this artist's works.

    A fact with a provenance, not a score: state is one of VISUAL_STATES,
    reason is the coarse code a classification may quote, and detail names the
    concrete check that decided it.  The counts are carried so a report can say
    "3 of 12 distinct works" instead of "insufficient".

    Only review_evidence() returns state="sufficient": it reads the stored review
    document and copies its identity into this object.  A caller that builds one
    by hand has no such document, cannot name a review_id, and is refused by
    __post_init__; a caller that round-trips one through as_dict()/from_dict()
    keeps the identity, and classify() still checks it against the rule set in
    use before believing it.
    """

    state: str
    reason: str = ""
    detail: str = ""
    distinct_works: int | None = None
    required_works: int | None = None
    available_works: int | None = None
    receipt_id: str = ""
    rules_version: str = ""
    snapshot_id: str = ""
    #: The stored review document that was actually read, and the fingerprint of
    #: the rule set the review was taken against.  Both are copied from the
    #: canonical record, never from the mapping a caller handed in: a receipt
    #: that has not been stored cannot name itself as evidence (RA-04).
    review_id: str = ""
    rules_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.state not in VISUAL_STATES:
            raise RuleError("visual_state_invalid")
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "detail", str(self.detail))
        for name in ("distinct_works", "required_works", "available_works"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise RuleError("visual_count_invalid", name)
        object.__setattr__(self, "receipt_id", str(self.receipt_id))
        object.__setattr__(self, "rules_version", str(self.rules_version))
        object.__setattr__(self, "snapshot_id", str(self.snapshot_id))
        object.__setattr__(self, "review_id", str(self.review_id))
        object.__setattr__(self, "rules_fingerprint", str(self.rules_fingerprint))
        if self.state == "sufficient" and not (
            self.receipt_id and self.snapshot_id and self.review_id
        ):
            # "Sufficient" is a claim about a specific receipt read from a
            # specific sample; without those identities it is an opinion with no
            # source, which is exactly what EA-07 was about.  review_id is the
            # stored document the claim was read from: a caller that types the
            # object by hand has no such document and is refused here (RA-04).
            raise RuleError("visual_evidence_unidentified")

    @property
    def sufficient(self) -> bool:
        return self.state == "sufficient"

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "distinct_works": self.distinct_works,
            "required_works": self.required_works,
            "available_works": self.available_works,
            "receipt_id": self.receipt_id,
            "rules_version": self.rules_version,
            "snapshot_id": self.snapshot_id,
            "review_id": self.review_id,
            "rules_fingerprint": self.rules_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> "VisualEvidence":
        """Rebuild the fact from as_dict() output.

        A round trip is a copy, not a new observation: the identity fields travel
        with it so a re-read value can still be checked against the stored review
        and the current rule set.  Anything that cannot be rebuilt raises, so a
        hand-written "sufficient" cannot sneak back in through a dictionary.
        """
        if not isinstance(value, Mapping):
            raise RuleError("visual_evidence_invalid")
        known = {
            "state", "reason", "detail", "distinct_works", "required_works",
            "available_works", "receipt_id", "rules_version", "snapshot_id",
            "review_id", "rules_fingerprint",
        }
        unknown = sorted(set(value) - known)
        if unknown:
            raise RuleError("visual_evidence_invalid", unknown[0])
        counts = {}
        for name in ("distinct_works", "required_works", "available_works"):
            item = value.get(name)
            if item is not None and type(item) is not int:
                raise RuleError("visual_count_invalid", name)
            counts[name] = item
        return cls(
            state=value.get("state"),
            reason=value.get("reason") or "",
            detail=value.get("detail") or "",
            receipt_id=value.get("receipt_id") or "",
            rules_version=value.get("rules_version") or "",
            snapshot_id=value.get("snapshot_id") or "",
            review_id=value.get("review_id") or "",
            rules_fingerprint=value.get("rules_fingerprint") or "",
            **counts,
        )


def _unknown(reason: str, detail: str, **counts) -> VisualEvidence:
    return VisualEvidence(state="unknown", reason=reason, detail=detail, **counts)


def _identity_view(review: Mapping) -> dict:
    """The fields a receipt and its stored copy must agree on, canonically.

    Only what identifies the observation: which account/artist/sample/snapshot
    it belongs to, which pages were looked at with which bytes, what the review
    concluded and which rule set it was taken against.  Presentation fields
    (observer name, rationale, proposed groups) are not identity: two copies of
    the same review may differ there without describing different observations.
    """
    viewed = review.get("viewed")
    evidence = review.get("evidence")
    rows = []
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if isinstance(item, Mapping):
                rows.append(
                    {
                        "illust_id": item.get("illust_id"),
                        "page": item.get("page"),
                        "sha256": item.get("sha256"),
                    }
                )
            else:
                rows.append(item)
    return {
        "id": review.get("id"),
        "account_id": review.get("account_id"),
        "artist_id": review.get("artist_id"),
        "snapshot_id": review.get("snapshot_id"),
        "sample_id": review.get("sample_id"),
        "viewed": list(viewed) if isinstance(viewed, (list, tuple)) else viewed,
        "evidence": rows if isinstance(evidence, (list, tuple)) else evidence,
        "needs_more": review.get("needs_more"),
        "rules_version": review.get("rules_version"),
        "rules_fingerprint": review.get("rules_fingerprint"),
        "attestation": review.get("attestation"),
    }


def _identity_digest(review: Mapping) -> str:
    encoded = json.dumps(
        _identity_view(review), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stored_review(store, review_id: str) -> tuple[Mapping | None, str]:
    """Read the canonical review document, or say why it cannot be read.

    A receipt is a *claim about a stored document*.  Until this function returns
    that document, nothing in the receipt proves anything: a dict can be typed by
    anyone, and copying the sample's hashes and the attestation string out of an
    existing sample is exactly how the RA-04 counterexample crossed the bar
    without a review ever being saved.
    """
    reader = getattr(store, "get", None)
    if not callable(reader):
        return None, "review_not_stored"
    try:
        stored = reader("review", review_id)
    except KeyError:
        return None, "review_not_stored"
    if not isinstance(stored, Mapping):
        return None, "review_not_a_mapping"
    return stored, ""


def review_evidence(
    store,
    receipt: object,
    *,
    current_rules_version: object = "",
    current_snapshot_id: object = "",
    current_rules_fingerprint: object = "",
) -> VisualEvidence:
    """Turn a stored review receipt into a visual-evidence fact.

    The receipt is not trusted because it exists: it has to be the stored
    document of the same id, and every claim is checked against the sample
    record it points at.  The sample's available_works is the stored inventory
    the sampler wrote from the website, so a caller cannot lower the bar by
    typing a smaller number.

    Checks, in the order a reviewer would ask them:
    1. the sample exists and the receipt names the same account/artist/snapshot;
    2. the receipt carries the explicit view attestation samples.review() writes;
    3. every viewed key exists in the sample, and its recorded sha256 and
       illust_id match the receipt's copy (a receipt for other bytes proves
       nothing about these bytes);
    4. the receipt is the canonical review stored under its id, and every
       identity field agrees with that stored copy (a retyped dict is not a
       review, and an edited copy is a different claim);
    5. distinct works (pages of one work collapse) reach min(12, available);
    6. the receipt is not stale for the rules/snapshot the caller is using, and
       it names the rule set it was taken against (a review with no rule
       fingerprint cannot answer for a current rule set).

    A failure is an answer, not an exception: it comes back as unknown,
    insufficient or stale with a detail, so a report can say why.  Only a
    malformed *argument* raises.
    """
    if not isinstance(receipt, Mapping):
        raise OrganizerError("receipt_not_a_mapping")
    sample_id = receipt.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        return _unknown("visual_evidence_unverified", "receipt_without_sample")
    try:
        sample = store.get("sample", sample_id)
    except KeyError:
        return _unknown("visual_evidence_unverified", "sample_unknown",
                        receipt_id=str(receipt.get("id") or ""))
    if not isinstance(sample, Mapping):
        return _unknown("visual_evidence_unverified", "sample_not_a_mapping")

    bound = tuple(str(sample.get(name) or "") for name in ("account_id", "artist_id", "snapshot_id"))
    declared = tuple(str(receipt.get(name) or "") for name in ("account_id", "artist_id", "snapshot_id"))
    receipt_id = str(receipt.get("id") or "")
    if not all(bound) or bound != declared:
        return _unknown("visual_evidence_unverified", "receipt_sample_mismatch",
                        receipt_id=receipt_id)
    if receipt.get("attestation") != REVIEW_ATTESTATION:
        return _unknown("visual_evidence_unverified", "view_attestation_missing",
                        receipt_id=receipt_id, snapshot_id=bound[2])

    viewed = receipt.get("viewed")
    images = sample.get("images")
    if (not isinstance(viewed, (list, tuple)) or not viewed
            or not isinstance(images, Mapping)):
        return _unknown("visual_evidence_unverified", "viewed_works_missing",
                        receipt_id=receipt_id, snapshot_id=bound[2])
    evidence = receipt.get("evidence")
    if not isinstance(evidence, (list, tuple)) or len(evidence) != len(viewed):
        return _unknown("visual_evidence_unverified", "receipt_evidence_mismatch",
                        receipt_id=receipt_id, snapshot_id=bound[2])

    works: set[str] = set()
    for position, page_key in enumerate(viewed):
        saved = images.get(page_key) if isinstance(page_key, str) else None
        copy = evidence[position]
        if not isinstance(saved, Mapping) or not isinstance(copy, Mapping):
            return _unknown("visual_evidence_unverified", "receipt_evidence_unknown_page",
                            receipt_id=receipt_id, snapshot_id=bound[2])
        saved_sha = str(saved.get("sha256") or "")
        if not saved_sha or str(copy.get("sha256") or "") != saved_sha:
            return _unknown("visual_evidence_unverified", "view_evidence_mismatch",
                            receipt_id=receipt_id, snapshot_id=bound[2])
        if str(copy.get("illust_id") or "") != str(saved.get("illust_id") or ""):
            return _unknown("visual_evidence_unverified", "view_evidence_mismatch",
                            receipt_id=receipt_id, snapshot_id=bound[2])
        illust = str(saved.get("illust_id") or "")
        if not illust:
            # A page without a work id cannot contribute a distinct work.
            return _unknown("visual_evidence_unverified", "receipt_evidence_unknown_page",
                            receipt_id=receipt_id, snapshot_id=bound[2])
        works.add(illust)

    # -- the receipt has to be the stored review, not a retyped copy -----------
    if not receipt_id:
        return _unknown("visual_evidence_unverified", "review_not_stored",
                        snapshot_id=bound[2], distinct_works=len(works))
    stored, stored_reason = _stored_review(store, receipt_id)
    if stored is None:
        return _unknown("visual_evidence_unverified", stored_reason,
                        receipt_id=receipt_id, snapshot_id=bound[2],
                        distinct_works=len(works))
    passed_digest = _identity_digest(receipt)
    stored_digest = _identity_digest(stored)
    if passed_digest != stored_digest:
        # Same id, different claim.  Neither copy is allowed to decide which one
        # is the review: the store holds the record, so the caller's copy is the
        # one that has to agree with it, and the two digests are reported.
        return _unknown(
            "visual_evidence_unverified",
            "receipt_copy_mismatch:%s:%s" % (passed_digest[:12], stored_digest[:12]),
            receipt_id=receipt_id, snapshot_id=bound[2], distinct_works=len(works),
        )

    available = sample.get("available_works")
    if type(available) is not int or available < 0:
        return _unknown("visual_evidence_unverified", "available_works_unknown",
                        receipt_id=receipt_id, review_id=receipt_id,
                        snapshot_id=bound[2], distinct_works=len(works))
    required = min(MAX_REQUIRED_WORKS, available)
    counts = {
        "distinct_works": len(works),
        "required_works": required,
        "available_works": available,
        "receipt_id": receipt_id,
        # Copied from the stored document: the canonical record says which rule
        # set this review was taken against, and it cannot be re-labelled by the
        # caller's copy (the identity comparison above would already refuse it).
        "rules_version": str(stored.get("rules_version") or ""),
        "rules_fingerprint": str(stored.get("rules_fingerprint") or ""),
        "review_id": receipt_id,
        "snapshot_id": bound[2],
    }

    stale = ""
    if current_rules_version and counts["rules_version"] != str(current_rules_version):
        stale = "stale_receipt"
    elif current_snapshot_id and bound[2] != str(current_snapshot_id):
        stale = "stale_snapshot"
    if stale:
        return VisualEvidence(state="stale", reason="visual_evidence_stale",
                              detail=stale, **counts)

    needs_more = stored.get("needs_more")
    if type(needs_more) is not bool:
        return _unknown("visual_evidence_unverified", "receipt_incomplete", **counts)
    if needs_more:
        return VisualEvidence(state="insufficient", reason="visual_evidence_insufficient",
                              detail="receipt_asked_for_more", **counts)
    if required == 0:
        return VisualEvidence(state="insufficient", reason="visual_evidence_insufficient",
                              detail="available_works_empty", **counts)
    if len(works) < required:
        return VisualEvidence(state="insufficient", reason="visual_evidence_insufficient",
                              detail="viewed_works_below_required", **counts)
    # A review answers "somebody looked at these works"; it answers "under which
    # rule set" only if it was recorded with that rule set's fingerprint.  An
    # older review (written before fingerprints existed) cannot be lifted onto a
    # current rule set by relabelling it, so its honest answer is unknown.
    if not counts["rules_fingerprint"]:
        return _unknown("visual_evidence_unverified", "visual_rules_unbound", **counts)
    if current_rules_fingerprint and counts["rules_fingerprint"] != str(current_rules_fingerprint):
        return VisualEvidence(state="stale", reason="visual_evidence_stale",
                              detail="stale_rules_fingerprint", **counts)
    return VisualEvidence(state="sufficient", reason="", detail="", **counts)


def _plain_visual(evidence: Mapping) -> VisualEvidence:
    """What a bare evidence mapping can say about viewing: never "sufficient".

    A dict is a claim, not a receipt.  Anyone can write
    {"viewed_works": ["1"]}, so the honest reading of a plain mapping is
    "unverified" (somebody said something) or "missing" (nobody did).  The
    distinct count is still reported, because it is useful to see how far a
    claim would be from the bar even when it cannot cross it.
    """
    raw = evidence.get("viewed_works")
    if raw is None:
        return VisualEvidence(state="unknown", reason="visual_evidence_missing",
                              detail="no_receipt")
    if not isinstance(raw, (list, tuple)):
        raise OrganizerError("viewed_works_invalid")
    if not raw:
        return VisualEvidence(state="unknown", reason="visual_evidence_missing",
                              detail="no_viewed_works")
    works = set()
    for item in raw:
        if isinstance(item, Mapping):
            named = item.get("illust_id", item.get("work_id"))
            works.add(str(named) if named is not None else "")
        else:
            works.add(str(item))
    works.discard("")
    return VisualEvidence(state="unknown", reason="visual_evidence_unverified",
                          detail="no_receipt", distinct_works=len(works))


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


def _strength(total_weight: int) -> str:
    """How strongly the rules matched.  A score about text, not about pictures."""
    return "high" if total_weight >= 2 else "low"


def _rules_binding(visual: VisualEvidence, ruleset: RuleSet) -> VisualEvidence:
    """The visual fact as *this* rule set may use it.

    A review answers "somebody looked at these works"; the fingerprint says which
    rule set that review was taken against.  Neither side may assume the two are
    the same because the version label looks familiar: the label is chosen by
    whoever wrote the review, the fingerprint is derived from the rules
    themselves, and a rule set whose content changed under an unchanged label is
    a different rule set.

    A fact that does not match is turned into stale *here*, at the point of
    consumption, so classify, changed_by_version and any future planner all see
    the same refusal instead of each trusting the object it was handed.
    """
    if not visual.sufficient:
        return visual
    if not visual.rules_fingerprint:
        detail = "visual_rules_unbound"
    elif visual.rules_version != ruleset.version:
        detail = "rules_version_moved"
    elif visual.rules_fingerprint != ruleset_fingerprint(ruleset):
        detail = "rules_content_moved"
    else:
        return visual
    return replace(
        visual, state="stale", reason="visual_evidence_stale", detail=detail
    )


def classify(
    evidence: object,
    ruleset: RuleSet,
    *,
    visual: VisualEvidence | None = None,
) -> dict:
    """Apply the rule set to already-collected evidence.  Reads only.

    Two questions, answered separately (EA-07, S0_CONTRACTS_R2.md section 7):

    * match_strength: how strongly the rules matched the tags.  This is what may
      suggest groups.
    * visual_evidence: whether a stored review receipt proves the works were
      viewed.  Only review_evidence() can answer "sufficient".

    confidence is high only when the match is strong AND the visual evidence is
    sufficient, so nobody can turn a tag hit into "somebody reviewed this
    artist" by adding weight to a rule.  "needs_more" therefore stays True for
    every unverified claim; the reasons say which of the two was missing.
    """
    if not isinstance(ruleset, RuleSet):
        raise RuleError("ruleset_required")
    if visual is not None and not isinstance(visual, VisualEvidence):
        raise RuleError("visual_evidence_invalid")
    if visual is None and isinstance(evidence, Mapping):
        carried = evidence.get("visual_evidence")
        if isinstance(carried, VisualEvidence):
            visual = carried
    tags = _tags(evidence)
    if visual is None:
        visual = _plain_visual(evidence)
    # Consumption-time gate (RA-04): a sufficient fact is only usable by the rule
    # set it was taken against, whatever constructor produced it.
    visual = _rules_binding(visual, ruleset)
    if not tags:
        return {
            "groups": [],
            "anchors": [],
            "confidence": "unknown",
            "needs_more": True,
            "match_strength": "unknown",
            "visual_evidence": visual.as_dict(),
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
            "match_strength": "unknown",
            "visual_evidence": visual.as_dict(),
            "reasons": ["no_rule_matched"],
        }
    ordered = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    reasons: list[str] = []
    if len(ordered) > MAX_GROUPS:
        ordered = ordered[:MAX_GROUPS]
        reasons.append("groups_truncated")
    strength = _strength(sum(weights.values()))
    if strength != "high":
        reasons.append("single_weak_hit")
    # A visual reason is only quoted when it is the thing standing between this
    # artist and "high": a weak match is already low for its own reason, and
    # listing every missing fact would blur which one the caller has to fix.
    if strength == "high" and not visual.sufficient and visual.reason:
        reasons.append(visual.reason)
    confidence = "high" if (strength == "high" and visual.sufficient) else "low"
    if confidence != "high":
        reasons.append("low_confidence")
    return {
        "groups": [name for name, _weight in ordered],
        "anchors": anchors,
        "confidence": confidence,
        "needs_more": confidence != "high",
        "match_strength": strength,
        "visual_evidence": visual.as_dict(),
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
