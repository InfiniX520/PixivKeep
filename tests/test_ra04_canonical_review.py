"""RA-04: a review proves what was stored, and only for the rule set it names.

The two independent counterexamples behind this module pull in opposite
directions, and that is worth stating plainly:

* `test_ra04_visual_receipt_cannot_be_reused_under_new_rule_version` (frozen,
  line 61-62) calls review_evidence() with a store that holds one sample and no
  review at all, and asserts `visual.sufficient`.  It was written against the
  pre-gate behaviour to reach its real target, which is line 65: v1 evidence must
  not lift a v2 rule set.
* `test_ra04_visual_adapter_requires_a_stored_review_not_a_retyped_mapping`
  (frozen, line 124-126) calls the same judgement with a real store that holds
  one sample and no review, and asserts `not visual.sufficient`.

One input, opposite expectations.  S0_CONTRACTS_R3 section 5 settles it: an
unretrievable review must be unverified, so the first counterexample's opening
assertion is obsolete and its *target* is covered here instead -- with a review
that really was stored.

Everything below uses the real organizer.store.Store and, where it matters, the
real samples.review() writer, so "stored" means stored.
"""
from __future__ import annotations

import hashlib

import pytest

from organizer.common import OrganizerError
from organizer.rules import (
    REVIEW_ATTESTATION,
    Rule,
    RuleError,
    RuleSet,
    VisualEvidence,
    changed_by_version,
    classify,
    review_evidence,
    ruleset_fingerprint,
)
from organizer.samples import review as write_review
from organizer.store import Store


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


def rules_v1():
    return RuleSet("v1", (Rule("r-cat", "topic", "cat", ("cats",), weight=2),))


def rules_v1_relabelled():
    """Same version label, different content: a different rule set."""
    return RuleSet("v1", (Rule("r-cat", "topic", "cat", ("cats",), weight=3),))


def rules_v2():
    return RuleSet("v2", (Rule("r-cat", "topic", "cat", ("cats",), weight=2),))


def image_record(illust, page, data, sample_id="s1"):
    key = "%s_p%d" % (illust, page)
    return key, {
        "illust_id": str(illust),
        "page": page,
        "sha256": sha256_of(data),
        "size": len(data),
        "media_type": "image/png",
        "relative_path": "samples/%s/%s.image" % (sample_id, key),
    }


def build_sample(tmp_path, *, works=12, pages=1, available=12, sample_id="s1"):
    """Write real files and the sample record samples.review() verifies against."""
    store = Store(tmp_path / "organizer.sqlite3")
    root = tmp_path / "data"
    images = {}
    selected = []
    for index in range(works):
        illust = str(1000 + index)
        selected.append(illust)
        for page in range(pages):
            data = ("%s:%d:synthetic" % (illust, page)).encode("utf-8")
            key, record = image_record(illust, page, data, sample_id)
            target = root / record["relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            images[key] = record
    record = {
        "id": sample_id,
        "account_id": "acct-1",
        "artist_id": "7",
        "snapshot_id": "snap-1",
        "requested": 16,
        "selected_ids": selected,
        "status": "ready_for_review",
        "images": images,
        "skipped": {},
    }
    if available is not None:
        record["available_works"] = available
    store.put("sample", sample_id, record)
    return store, root, record


def store_record(store, receipt):
    """Store a review document as an older version of the writer might have.

    samples.review() refuses to save an incomplete review, so the reader's
    shortfall branches can only be exercised on records that already exist in a
    store written by something else.  The store accepts a new id, not an
    overwrite, so nothing here can rewrite a real review.
    """
    store.put("review", receipt["id"], receipt)
    return receipt


def payload_for(sample, keys=None, **extra):
    chosen = list(sample["images"]) if keys is None else list(keys)
    payload = {
        "sample_id": sample["id"],
        "viewed": [
            {"illust_id": sample["images"][key]["illust_id"],
             "page": sample["images"][key]["page"],
             "sha256": sample["images"][key]["sha256"]}
            for key in chosen
        ],
        "observer": "synthetic",
        "rationale": "synthetic review of the listed pages",
        "coverage_note": "distinct works across the selected range",
        "rules_version": "v1",
        "proposed_groups": ["cats"],
        "needs_more": False,
    }
    payload.update(extra)
    return payload


# -- 1. the storage gate ------------------------------------------------------


def test_a_receipt_without_a_stored_review_cannot_prove_anything(tmp_path):
    # The frozen counterexample #2, restated on the real store.
    store, _root, sample = build_sample(tmp_path, works=12)
    receipt = {
        "id": "r1",
        "account_id": sample["account_id"],
        "artist_id": sample["artist_id"],
        "snapshot_id": sample["snapshot_id"],
        "sample_id": sample["id"],
        "viewed": list(sample["images"]),
        "evidence": [dict(sample["images"][key]) for key in sample["images"]],
        "needs_more": False,
        "rules_version": "v1",
        "rules_fingerprint": ruleset_fingerprint(rules_v1()),
        "attestation": REVIEW_ATTESTATION,
    }
    assert store.list("review") == []
    fact = review_evidence(store, receipt, current_rules_version="v1",
                           current_snapshot_id=sample["snapshot_id"])
    assert fact.state == "unknown"
    assert fact.detail == "review_not_stored"
    assert not fact.sufficient
    assert classify({"tags": ["cat"]}, rules_v1(), visual=fact)["confidence"] == "low"
    store.close()


def test_the_stored_review_is_what_proves_the_works_were_viewed(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                  rules_fingerprint=rules),
                          attest_viewed=True)
    assert store.get("review", stored["id"])["rules_fingerprint"] == ruleset_fingerprint(rules)
    fact = review_evidence(store, stored, current_rules_version=rules.version,
                           current_snapshot_id=sample["snapshot_id"],
                           current_rules_fingerprint=ruleset_fingerprint(rules))
    assert fact.state == "sufficient"
    assert fact.review_id == stored["id"]
    assert fact.rules_fingerprint == ruleset_fingerprint(rules)
    result = classify({"tags": ["cat"]}, rules, visual=fact)
    assert result["confidence"] == "high"
    assert result["needs_more"] is False
    store.close()


def test_a_retyped_copy_of_a_stored_review_is_refused(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                  rules_fingerprint=rules),
                          attest_viewed=True)
    copy = dict(stored)
    copy["needs_more"] = True
    fact = review_evidence(store, copy)
    assert fact.state == "unknown"
    assert fact.detail.startswith("receipt_copy_mismatch:")
    assert not fact.sufficient
    # Neither copy rewrote the record: the store still holds the writer's.
    assert store.get("review", stored["id"]) == stored
    assert len(store.list("review")) == 1
    store.close()


def test_a_copy_that_changes_the_rule_label_is_refused_before_staleness(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                  rules_fingerprint=rules),
                          attest_viewed=True)
    copy = dict(stored)
    copy["rules_version"] = "v9"
    fact = review_evidence(store, copy)
    assert fact.state == "unknown"
    assert fact.detail.startswith("receipt_copy_mismatch:")
    store.close()


# -- 2. the identity checks that already existed still run --------------------


def test_a_stored_review_pointing_at_another_artist_is_refused(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                   rules_fingerprint=rules),
                          attest_viewed=True)
    copy = dict(stored, artist_id="8")
    fact = review_evidence(store, copy)
    assert fact.state == "unknown"
    assert fact.detail == "receipt_sample_mismatch"
    store.close()


def test_a_review_whose_sample_has_other_bytes_proves_nothing(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                   rules_fingerprint=rules),
                          attest_viewed=True)
    # The sample is re-read after the review was written (the website showed
    # other bytes for that page), so the stored review no longer describes it.
    moved = dict(sample["images"])
    first = sorted(moved)[0]
    moved[first] = dict(moved[first], sha256=sha256_of(b"other bytes"))
    updated = dict(sample, images=moved)
    store.put("sample", sample["id"], updated)
    fact = review_evidence(store, stored)
    assert fact.state == "unknown"
    assert fact.detail == "view_evidence_mismatch"
    store.close()


def test_pages_of_one_work_still_count_once(tmp_path):
    store, root, sample = build_sample(tmp_path, works=3, pages=4, available=12)
    rules = rules_v1()
    stored = store_record(store, {
        "id": "r1",
        "account_id": sample["account_id"],
        "artist_id": sample["artist_id"],
        "snapshot_id": sample["snapshot_id"],
        "sample_id": sample["id"],
        "viewed": list(sample["images"]),
        "evidence": [dict(sample["images"][key]) for key in sample["images"]],
        "needs_more": False,
        "rules_version": rules.version,
        "rules_fingerprint": ruleset_fingerprint(rules),
        "attestation": REVIEW_ATTESTATION,
    })
    fact = review_evidence(store, stored)
    assert (fact.distinct_works, fact.required_works) == (3, 12)
    assert fact.state == "insufficient"
    assert fact.detail == "viewed_works_below_required"
    store.close()


def test_an_unknown_inventory_stays_unknown(tmp_path):
    store, root, sample = build_sample(tmp_path, works=3, available=None)
    rules = rules_v1()
    stored = store_record(store, {
        "id": "r1",
        "account_id": sample["account_id"],
        "artist_id": sample["artist_id"],
        "snapshot_id": sample["snapshot_id"],
        "sample_id": sample["id"],
        "viewed": list(sample["images"]),
        "evidence": [dict(sample["images"][key]) for key in sample["images"]],
        "needs_more": False,
        "rules_version": rules.version,
        "rules_fingerprint": ruleset_fingerprint(rules),
        "attestation": REVIEW_ATTESTATION,
    })
    fact = review_evidence(store, stored)
    assert fact.state == "unknown"
    assert fact.detail == "available_works_unknown"
    assert fact.required_works is None
    store.close()


def test_a_snapshot_update_makes_the_review_stale(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                   rules_fingerprint=rules),
                          attest_viewed=True)
    fact = review_evidence(store, stored, current_snapshot_id="snap-2")
    assert fact.state == "stale"
    assert fact.detail == "stale_snapshot"
    assert classify({"tags": ["cat"]}, rules, visual=fact)["confidence"] == "low"
    store.close()


# -- 3. the target of the obsolete counterexample ----------------------------


def test_v1_evidence_cannot_lift_a_v2_rule_set(tmp_path):
    """Frozen counterexample #1's real target, with the review actually stored.

    v1 evidence is a fact about the pictures; v2 is a different rule set, and the
    consumer must not lift it to high just because the fact says "viewed".
    """
    store, root, sample = build_sample(tmp_path, works=12)
    old = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=old.version,
                                                   rules_fingerprint=old),
                          attest_viewed=True)
    fact = review_evidence(store, stored, current_rules_version=old.version,
                           current_snapshot_id=sample["snapshot_id"],
                           current_rules_fingerprint=ruleset_fingerprint(old))
    assert fact.state == "sufficient"
    assert classify({"tags": ["cat"]}, old, visual=fact)["confidence"] == "high"

    result = classify({"tags": ["cat"]}, rules_v2(), visual=fact)
    assert result["needs_more"] is True
    assert result["confidence"] == "low"
    assert result["visual_evidence"]["state"] == "stale"
    assert result["visual_evidence"]["detail"] == "rules_version_moved"
    assert "visual_evidence_stale" in result["reasons"]
    store.close()


def test_the_same_version_label_with_other_rules_is_another_rule_set(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    old = rules_v1()
    relabelled = rules_v1_relabelled()
    assert old.version == relabelled.version
    assert ruleset_fingerprint(old) != ruleset_fingerprint(relabelled)
    stored = write_review(store, root, payload_for(sample, rules_version=old.version,
                                                   rules_fingerprint=old),
                          attest_viewed=True)
    fact = review_evidence(store, stored)
    assert fact.state == "sufficient"
    result = classify({"tags": ["cat"]}, relabelled, visual=fact)
    assert result["confidence"] == "low"
    assert result["needs_more"] is True
    assert result["visual_evidence"]["detail"] == "rules_content_moved"
    store.close()


def test_a_review_from_before_fingerprints_is_unknown_not_sufficient(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    stored = write_review(store, root, payload_for(sample), attest_viewed=True)
    assert stored["rules_fingerprint"] == ""
    fact = review_evidence(store, stored)
    assert fact.state == "unknown"
    assert fact.detail == "visual_rules_unbound"
    assert not fact.sufficient
    result = classify({"tags": ["cat"]}, rules_v1(), visual=fact)
    assert result["confidence"] == "low"
    store.close()


def test_changed_by_version_applies_the_same_gate(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    old = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=old.version,
                                                   rules_fingerprint=old),
                          attest_viewed=True)
    fact = review_evidence(store, stored)
    per_artist = {"7": {"tags": ["cat"], "visual_evidence": fact}}
    moved = changed_by_version(per_artist, old, rules_v2())
    assert list(moved) == ["7"]
    assert moved["7"]["before"]["confidence"] == "high"
    assert moved["7"]["after"]["confidence"] == "low"
    # Same rule content under another label changes nothing a reviewer claimed.
    same = changed_by_version(per_artist, old, RuleSet("v1", old.rules))
    assert same == {}
    store.close()


# -- 4. round trip and refusal shapes ---------------------------------------


def test_a_round_trip_keeps_the_identity_and_the_gate(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    rules = rules_v1()
    stored = write_review(store, root, payload_for(sample, rules_version=rules.version,
                                                   rules_fingerprint=rules),
                          attest_viewed=True)
    fact = review_evidence(store, stored)
    again = VisualEvidence.from_dict(fact.as_dict())
    assert again == fact
    assert again.as_dict()["review_id"] == stored["id"]
    assert classify({"tags": ["cat"]}, rules, visual=again)["confidence"] == "high"
    assert classify({"tags": ["cat"]}, rules_v2(), visual=again)["confidence"] == "low"
    store.close()


def test_a_forged_sufficient_without_a_review_id_is_refused():
    with pytest.raises(RuleError) as caught:
        VisualEvidence(state="sufficient", receipt_id="r1", snapshot_id="snap-1")
    assert caught.value.code == "visual_evidence_unidentified"
    with pytest.raises(RuleError) as caught:
        VisualEvidence.from_dict({"state": "sufficient", "receipt_id": "r1",
                                  "snapshot_id": "snap-1", "review_id": ""})
    assert caught.value.code == "visual_evidence_unidentified"


def test_from_dict_refuses_keys_it_does_not_understand():
    value = VisualEvidence(state="unknown", reason="visual_evidence_missing").as_dict()
    value["state"] = "sufficient"
    value["review_id"] = "r1"
    value["receipt_id"] = "r1"
    value["snapshot_id"] = "snap-1"
    value["invented"] = "sure"
    with pytest.raises(RuleError) as caught:
        VisualEvidence.from_dict(value)
    assert caught.value.code == "visual_evidence_invalid"


def test_a_review_for_another_store_cannot_be_replayed(tmp_path):
    first, root, sample = build_sample(tmp_path / "a", works=12)
    rules = rules_v1()
    stored = write_review(first, root, payload_for(sample, rules_version=rules.version,
                                                   rules_fingerprint=rules),
                          attest_viewed=True)
    second, _root2, sample2 = build_sample(tmp_path / "b", works=12)
    assert second.get("sample", "s1")["images"] == sample2["images"]
    fact = review_evidence(second, stored)
    assert fact.state == "unknown"
    assert fact.detail == "review_not_stored"
    first.close()
    second.close()


def test_a_malformed_fingerprint_is_refused_at_write_time(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    with pytest.raises(OrganizerError) as caught:
        write_review(store, root, payload_for(sample, rules_fingerprint="not-a-digest"),
                     attest_viewed=True)
    assert caught.value.code == "rules_fingerprint_invalid"
    assert store.list("review") == []
    store.close()


def test_a_rule_set_and_a_mismatched_version_label_are_refused(tmp_path):
    store, root, sample = build_sample(tmp_path, works=12)
    with pytest.raises(OrganizerError) as caught:
        write_review(store, root, payload_for(sample, rules_version="v9",
                                              rules_fingerprint=rules_v1()),
                     attest_viewed=True)
    assert caught.value.code == "rules_version_mismatch"
    assert store.list("review") == []
    store.close()
