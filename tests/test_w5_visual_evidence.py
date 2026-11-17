"""EA-07: a tag score is not a look at the pictures (S0_CONTRACTS_R2 section 7).

These tests build the *same* facts organizer/samples.py writes: a sample record
whose available_works came from the website inventory, and a review receipt with
the explicit view attestation.  Nothing here invents a second rule: the bar is
min(12, available_works) distinct works, which is what samples.review() enforces.
"""
from __future__ import annotations

import hashlib

import pytest

from organizer.common import OrganizerError
from organizer.rules import (
    MAX_REQUIRED_WORKS,
    REVIEW_ATTESTATION,
    Rule,
    RuleError,
    RuleSet,
    VisualEvidence,
    classify,
    review_evidence,
    ruleset_fingerprint,
)


class Store:
    """The only reads review_evidence() may make: one sample, one review.

    A whitelist double, not a database: any *other* read is a bug in the module
    under test, so it stays an AssertionError.  Reviews are placed in it the way
    samples.review() places them in the real store, which is what makes "the
    receipt was never stored" a fact this file can state at all (RA-04): a dict
    that nobody stored cannot be proved by retyping it.
    """

    def __init__(self, samples, reviews=None):
        self.samples = dict(samples)
        self.reviews = dict(reviews or {})

    def get(self, kind, document_id):
        if kind == "sample":
            try:
                return self.samples[document_id]
            except KeyError:
                raise KeyError(document_id) from None
        if kind == "review":
            try:
                return self.reviews[document_id]
            except KeyError:
                raise KeyError(document_id) from None
        raise AssertionError("review_evidence read %r, not a sample or review" % (kind,))

    def store_review(self, review):
        """Put one review where samples.review() would have put it."""
        self.reviews[review["id"]] = review
        return review


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_sample(*, works=12, available="same", pages_per_work=1, sample_id="s1"):
    """A sample record shaped like samples.sample() writes it."""
    images = {}
    for index in range(works):
        illust = str(1000 + index)
        for page in range(pages_per_work):
            key = "%s_p%d" % (illust, page)
            images[key] = {
                "illust_id": illust,
                "page": page,
                "sha256": sha(key),
                "size": 12,
                "media_type": "image/png",
                "relative_path": "samples/%s/%s.image" % (sample_id, key),
            }
    record = {
        "id": sample_id,
        "account_id": "acct-1",
        "artist_id": "7",
        "snapshot_id": "snap-1",
        "requested": 16,
        "selected_ids": [str(1000 + index) for index in range(works)],
        "status": "ready_for_review",
        "images": images,
        "skipped": {},
    }
    if available is not None:
        record["available_works"] = works if available == "same" else available
    return record


def make_receipt(sample, keys=None, *, needs_more=False, attestation=REVIEW_ATTESTATION,
                 rules_version="v1", rules_fingerprint="", account="acct-1", artist="7",
                 snapshot=None, tamper_sha=False, tamper_illust=False, store=None):
    """A receipt shaped like samples.review() writes it.

    With store= the receipt is also *stored*, which is what makes it evidence at
    all: review_evidence() reads the canonical review under this id and refuses a
    copy that was never saved.  Without store= the dict is only a claim, and the
    caller is testing exactly that.
    """
    chosen = list(sample["images"]) if keys is None else list(keys)
    evidence = []
    for key in chosen:
        saved = dict(sample["images"][key])
        if tamper_sha:
            saved["sha256"] = sha("other bytes")
        if tamper_illust:
            saved["illust_id"] = "9999"
        evidence.append(saved)
    receipt = {
        "id": "r1",
        "created_at": "2026-11-15T00:00:00Z",
        "account_id": account,
        "artist_id": artist,
        "snapshot_id": snapshot or sample["snapshot_id"],
        "sample_id": sample["id"],
        "viewed": chosen,
        "evidence": evidence,
        "observer": "synthetic",
        "rationale": "",
        "rules_version": rules_version,
        "rules_fingerprint": rules_fingerprint,
        "coverage_note": "",
        "proposed_groups": [],
        "needs_more": needs_more,
        "attestation": attestation,
    }
    if store is not None:
        store.store_review(receipt)
    return receipt


def ruleset(version="1"):
    return RuleSet(version, (Rule("r-cat", "topic", "cat", ("cats",), weight=2),))


# -- the frozen counterexample, restated here so this file stands alone --------


def test_one_tag_without_viewed_works_cannot_be_high_confidence():
    result = classify({"tags": ["cat"], "viewed_works": []}, ruleset())
    assert result["needs_more"] is True
    assert result["confidence"] != "high"
    assert result["match_strength"] == "high"
    assert result["visual_evidence"]["state"] == "unknown"
    assert result["reasons"][0] == "visual_evidence_missing"


def test_a_plain_dict_can_never_prove_a_review():
    # Twelve claimed works in a dict is still a claim: no receipt, no proof.
    claimed = {"tags": ["cat"], "available_works": 12,
               "viewed_works": [{"illust_id": str(1000 + n)} for n in range(12)]}
    result = classify(claimed, ruleset())
    assert result["confidence"] == "low"
    assert result["visual_evidence"]["state"] == "unknown"
    assert result["visual_evidence"]["detail"] == "no_receipt"
    assert result["visual_evidence"]["distinct_works"] == 12


def test_a_visual_evidence_of_the_wrong_type_is_refused():
    with pytest.raises(RuleError) as caught:
        classify({"tags": ["cat"]}, ruleset(), visual={"state": "sufficient"})
    assert caught.value.code == "visual_evidence_invalid"


def test_a_hand_built_state_outside_the_vocabulary_is_refused():
    with pytest.raises(RuleError) as caught:
        VisualEvidence(state="certain")
    assert caught.value.code == "visual_state_invalid"


def test_sufficient_without_a_receipt_identity_is_refused():
    # The value that lifts a classification has to name the receipt and the
    # snapshot it came from; an anonymous "sufficient" is an opinion.
    with pytest.raises(RuleError) as caught:
        VisualEvidence(state="sufficient")
    assert caught.value.code == "visual_evidence_unidentified"


def test_review_evidence_refuses_something_that_is_not_a_receipt():
    with pytest.raises(OrganizerError) as caught:
        review_evidence(Store({}), ["not", "a", "mapping"])
    assert caught.value.code == "receipt_not_a_mapping"


# -- the store path: what makes a receipt evidence ----------------------------


def test_a_full_receipt_is_sufficient_and_lifts_the_classification():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    rules = ruleset(version="v1")
    receipt = make_receipt(sample, store=store, rules_version=rules.version,
                           rules_fingerprint=ruleset_fingerprint(rules))
    fact = review_evidence(store, receipt)
    assert fact.state == "sufficient"
    assert fact.distinct_works == 12
    assert fact.required_works == 12
    assert fact.available_works == 12
    assert fact.review_id == "r1"
    assert fact.rules_fingerprint == ruleset_fingerprint(rules)
    result = classify({"tags": ["cat"]}, rules, visual=fact)
    assert result["confidence"] == "high"
    assert result["needs_more"] is False
    assert result["reasons"] == []


def test_pages_of_one_work_count_once():
    sample = make_sample(works=3, available=12, pages_per_work=4)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, store=store))
    assert len(sample["images"]) == 12
    assert fact.distinct_works == 3
    assert fact.state == "insufficient"
    assert fact.detail == "viewed_works_below_required"
    assert classify({"tags": ["cat"]}, ruleset(), visual=fact)["confidence"] == "low"


def test_eleven_of_twelve_distinct_works_is_insufficient():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    keys = list(sample["images"])[:11]
    fact = review_evidence(store, make_receipt(sample, keys, store=store))
    assert fact.state == "insufficient"
    assert (fact.distinct_works, fact.required_works) == (11, 12)


def test_a_confirmed_three_work_artist_is_judged_against_its_own_inventory():
    # The bar is min(12, available), so a small artist can pass with 3 works.
    # available_works is the stored inventory, not something the caller types.
    sample = make_sample(works=3)
    store = Store({"s1": sample})
    rules = ruleset(version="v1")
    receipt = make_receipt(sample, store=store, rules_version=rules.version,
                           rules_fingerprint=ruleset_fingerprint(rules))
    fact = review_evidence(store, receipt)
    assert (fact.distinct_works, fact.required_works, fact.available_works) == (3, 3, 3)
    assert fact.state == "sufficient"
    assert classify({"tags": ["cat"]}, rules, visual=fact)["confidence"] == "high"


def test_an_unknown_inventory_is_unknown_not_a_lowered_bar():
    sample = make_sample(works=3, available=None)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, store=store))
    assert fact.state == "unknown"
    assert fact.detail == "available_works_unknown"
    assert fact.required_works is None
    result = classify({"tags": ["cat"]}, ruleset(), visual=fact)
    assert result["confidence"] == "low"
    assert result["visual_evidence"]["reason"] == "visual_evidence_unverified"


def test_an_artist_with_no_works_cannot_be_certified():
    sample = make_sample(works=0, available=0)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, store=store))
    assert fact.state in ("unknown", "insufficient")
    assert fact.detail in ("viewed_works_missing", "available_works_empty")
    assert classify({"tags": ["cat"]}, ruleset(), visual=fact)["confidence"] != "high"


def test_a_receipt_for_other_bytes_proves_nothing():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, tamper_sha=True, store=store))
    assert fact.state == "unknown"
    assert fact.detail == "view_evidence_mismatch"


def test_a_receipt_for_another_work_id_proves_nothing():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, tamper_illust=True, store=store))
    assert fact.state == "unknown"
    assert fact.detail == "view_evidence_mismatch"


def test_a_receipt_without_the_attestation_proves_nothing():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, attestation="", store=store))
    assert fact.state == "unknown"
    assert fact.detail == "view_attestation_missing"


def test_a_receipt_from_another_artist_is_refused():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, artist="8", store=store))
    assert fact.state == "unknown"
    assert fact.detail == "receipt_sample_mismatch"


def test_a_receipt_the_store_never_saw_is_unknown():
    sample = make_sample(works=12)
    store = Store({})
    fact = review_evidence(store, make_receipt(sample, store=store))
    assert fact.state == "unknown"
    assert fact.detail == "sample_unknown"


def test_a_receipt_that_itself_asked_for_more_cannot_certify():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, needs_more=True, store=store))
    assert fact.state == "insufficient"
    assert fact.detail == "receipt_asked_for_more"


def test_a_stale_rules_version_makes_the_receipt_stale():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(
        store, make_receipt(sample, rules_version="v1", store=store), current_rules_version="v2"
    )
    assert fact.state == "stale"
    assert fact.detail == "stale_receipt"
    result = classify({"tags": ["cat"]}, ruleset(), visual=fact)
    assert result["confidence"] == "low"
    assert "visual_evidence_stale" in result["reasons"]


def test_a_receipt_from_another_snapshot_is_stale():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    fact = review_evidence(store, make_receipt(sample, store=store), current_snapshot_id="snap-2")
    assert fact.state == "stale"
    assert fact.detail == "stale_snapshot"


def test_the_same_receipt_read_twice_gives_the_same_fact():
    sample = make_sample(works=12)
    store = Store({"s1": sample})
    receipt = make_receipt(sample, store=store)
    assert review_evidence(store, receipt) == review_evidence(store, receipt)
    assert review_evidence(store, receipt).as_dict()["receipt_id"] == "r1"


def test_the_bar_is_the_one_samples_review_enforces():
    # A drift between "what the sampler asks for" and "what the classifier
    # trusts" would be a second truth, so the constant is checked against the
    # gate that already exists instead of being restated from memory.
    import inspect

    from organizer import samples

    source = inspect.getsource(samples.review).replace(" ", "")
    assert ("min(12,data['available_works'])" in source
            or "min(MAX_REQUIRED_WORKS" in source)
    assert MAX_REQUIRED_WORKS == 12
