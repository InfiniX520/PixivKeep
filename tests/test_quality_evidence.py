"""C1: five quality dimensions stay five facts.

The cases below pin the vocabulary, the refusal of a state that belongs to a
different dimension, and the two properties the round depends on:

* `capability_unavailable` is a first class decode answer, never `ok`;
* nothing on `QualityEvidence` collapses the five dimensions into one boolean.

No file is read here: the module is pure data.
"""
from __future__ import annotations

import dataclasses

import pytest

from core.quality_evidence import (
    DECODE_OUTCOMES,
    DIMENSIONS,
    QUALITY_STATES,
    DecodeReceipt,
    Dimension,
    QualityEvidence,
    QualityEvidenceError,
    is_known_state,
    states_of,
)


def evidence(**overrides: str) -> QualityEvidence:
    """A fully specified record; callers replace only the dimension they test."""
    values = {
        "source": Dimension("declared_original"),
        "transport": Dimension("length_matched"),
        "structure": Dimension("ok", "png_structure_ok"),
        "decode": Dimension("ok", "decoded"),
        "pages": Dimension("all_frames"),
    }
    values.update({name: Dimension(state) for name, state in overrides.items()})
    return QualityEvidence(**values)


def test_the_vocabulary_is_closed_and_dimension_specific():
    assert DIMENSIONS == ("source", "transport", "structure", "decode", "pages")
    assert states_of("source") == ("declared_original", "unknown")
    assert states_of("decode") == ("ok", "failed", "undecided", "capability_unavailable")
    assert states_of("nope") == ()
    assert is_known_state("decode", "capability_unavailable") is True
    assert is_known_state("structure", "capability_unavailable") is False
    assert is_known_state("source", "ok") is False
    assert is_known_state("decode", ["ok"]) is False


def test_a_complete_record_round_trips_through_its_dict():
    original = evidence(structure="undecided", decode="failed", pages="first_frame_only")
    payload = original.as_dict()
    assert list(payload) == list(DIMENSIONS)
    assert payload["decode"] == {"state": "failed", "reason": None}
    restored = QualityEvidence.from_dict(payload)
    assert restored == original
    assert restored.as_dict() == payload


def test_an_illegal_state_is_refused_by_the_dimension_that_owns_the_field():
    for name, bad in (
        ("source", "ok"),
        ("transport", "all_frames"),
        ("structure", "capability_unavailable"),
        ("decode", "all_frames"),
        ("pages", "declared_original"),
    ):
        with pytest.raises(QualityEvidenceError) as caught:
            evidence(**{name: bad})
        assert caught.value.code == "dimension_state_invalid"
        assert name in str(caught.value)


def test_a_bare_dimension_only_checks_its_own_shape():
    # Legal on its own: the field it is placed in decides the vocabulary.
    assert Dimension("ok").state == "ok"
    with pytest.raises(QualityEvidenceError) as caught:
        QualityEvidence(
            source=Dimension("ok"),
            transport=Dimension("length_matched"),
            structure=Dimension("ok"),
            decode=Dimension("ok"),
            pages=Dimension("all_frames"),
        )
    assert caught.value.code == "dimension_state_invalid"


def test_states_and_reasons_are_refused_when_they_are_not_trimmed_text():
    for bad in ("", "  ", " ok", "ok "):
        with pytest.raises(QualityEvidenceError) as caught:
            Dimension(bad)
        assert caught.value.code == "dimension_state_invalid"
    with pytest.raises(QualityEvidenceError) as caught:
        Dimension(1)  # type: ignore[arg-type]
    assert caught.value.code == "dimension_state_invalid"
    for bad_reason in ("", " ", 3):
        with pytest.raises(QualityEvidenceError) as caught:
            Dimension("ok", bad_reason)  # type: ignore[arg-type]
        assert caught.value.code == "dimension_reason_invalid"


def test_capability_unavailable_is_a_decode_answer_and_never_ok():
    record = evidence(structure="ok", decode="capability_unavailable", pages="unknown")
    payload = record.as_dict()
    assert payload["structure"]["state"] == "ok"
    assert payload["decode"]["state"] == "capability_unavailable"
    assert payload["decode"]["state"] != payload["structure"]["state"]
    # The same record cannot be read as "decoded": the field is the evidence.
    assert record.states()["decode"] == "capability_unavailable"
    assert "capability_unavailable" in DECODE_OUTCOMES


def test_the_five_dimensions_are_not_folded_into_one_boolean():
    record = evidence(transport="unknown", decode="undecided")
    for collapsed in ("ok", "passed", "verified", "is_ok", "all_ok", "verdict"):
        assert not hasattr(record, collapsed), collapsed
    payload = record.as_dict()
    assert set(payload) == set(DIMENSIONS)
    assert all(type(payload[name]) is dict for name in DIMENSIONS)
    assert all(type(value) is not bool for value in payload.values())
    # Each dimension can disagree with every other one.
    assert len({payload[name]["state"] for name in DIMENSIONS}) == 5


def test_records_are_frozen_and_hashable():
    record = evidence()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.decode = Dimension("failed")  # type: ignore[misc]
    assert hash(record) == hash(evidence())


def test_from_dict_refuses_a_record_that_is_not_one():
    payload = evidence().as_dict()
    for broken in ({}, {"source": {"state": "unknown"}}, dict(payload, extra={"state": "ok"})):
        with pytest.raises(QualityEvidenceError) as caught:
            QualityEvidence.from_dict(broken)
        assert caught.value.code == "evidence_shape_invalid"
    with pytest.raises(QualityEvidenceError):
        QualityEvidence.from_dict("not a mapping")
    with pytest.raises(QualityEvidenceError) as caught:
        QualityEvidence.from_dict(dict(payload, source={"state": "ok"}))
    assert caught.value.code == "dimension_state_invalid"
    with pytest.raises(QualityEvidenceError) as caught:
        QualityEvidence.from_dict(dict(payload, source={"state": "unknown", "why": "x"}))
    assert caught.value.code == "dimension_shape_invalid"


def test_a_receipt_records_a_real_attempt_and_copies_its_limits():
    limits = {"max_bytes": 1 << 20, "max_pixels": 4096 * 4096, "max_seconds": 2.5}
    receipt = DecodeReceipt(
        format="png",
        frames=3,
        pixels=4096,
        limits=limits,
        decoder="stdlib-zlib-png",
        outcome="ok",
        seconds=0.125,
        checked_at="2026-11-14T14:00:00+00:00",
    )
    limits["max_bytes"] = 1  # the receipt must not follow the caller's dict
    payload = receipt.as_dict()
    assert payload["limits"]["max_bytes"] == 1 << 20
    assert payload["seconds"] == 0.125 and type(payload["seconds"]) is float
    payload["limits"]["max_bytes"] = 9  # nor may the caller edit the receipt
    assert receipt.as_dict()["limits"]["max_bytes"] == 1 << 20
    assert DecodeReceipt.from_dict(receipt.as_dict()) == receipt


def test_a_receipt_refuses_impossible_measurements():
    base = dict(
        format="png",
        frames=1,
        pixels=1,
        limits={"max_bytes": 10},
        decoder="stdlib-zlib-png",
        outcome="ok",
        seconds=0.0,
        checked_at="2026-11-14T14:00:00+00:00",
    )
    for name, bad in (
        ("frames", -1),
        ("pixels", -1),
        ("seconds", -0.5),
        ("format", ""),
        ("decoder", ""),
        ("checked_at", " "),
        ("limits", {"max_bytes": -1}),
        ("limits", "not-a-mapping"),
    ):
        with pytest.raises(QualityEvidenceError) as caught:
            DecodeReceipt(**dict(base, **{name: bad}))
        assert caught.value.code == "receipt_field_invalid", name
    with pytest.raises(QualityEvidenceError) as caught:
        DecodeReceipt(**dict(base, outcome="looks_fine"))
    assert caught.value.code == "receipt_outcome_invalid"
    # An attempt that could not run is still a receipt.
    assert DecodeReceipt(**dict(base, frames=0, pixels=0, outcome="capability_unavailable"))


def test_a_receipt_from_dict_refuses_a_missing_field():
    payload = DecodeReceipt(
        format="png",
        frames=1,
        pixels=1,
        limits={},
        decoder="stdlib-zlib-png",
        outcome="ok",
        seconds=0.0,
        checked_at="2026-11-14T14:00:00+00:00",
    ).as_dict()
    for broken in ({}, dict(payload, extra=1)):
        with pytest.raises(QualityEvidenceError) as caught:
            DecodeReceipt.from_dict(broken)
        assert caught.value.code == "receipt_shape_invalid"


def test_the_module_reports_the_same_vocabulary_the_design_freezes():
    assert QUALITY_STATES["structure"] == ("ok", "failed", "undecided", "unsupported")
    assert QUALITY_STATES["pages"] == ("all_frames", "first_frame_only", "unknown")
    assert QUALITY_STATES["transport"] == ("length_matched", "unknown")
    assert QUALITY_STATES["source"] == ("declared_original", "unknown")
