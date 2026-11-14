"""The closed vocabulary of filter reasons (U2-B, lane B1)."""
from __future__ import annotations

import json

import pytest

from core import filter_reasons as fr


def test_the_closed_sets_keep_the_documented_order():
    assert fr.REASON_CODES == (
        "favorites_below",
        "ai_excluded",
        "type_unsupported",
        "field_unknown",
        "access_denied",
    )
    assert fr.FIELDS == (
        "bookmarks",
        "ai_flag",
        "type",
        "availability",
        "original_urls",
    )


def test_a_reason_is_immutable_and_still_serialises_as_json():
    record = fr.reason("favorites_below", "bookmarks", 12)
    assert dict(record) == {
        "code": "favorites_below",
        "field": "bookmarks",
        "evidence": 12,
        "unknown": False,
    }
    assert json.loads(json.dumps(record))["evidence"] == 12
    with pytest.raises(TypeError):
        record["code"] = "ai_excluded"
    with pytest.raises(TypeError):
        record.update({"code": "ai_excluded"})
    with pytest.raises(TypeError):
        record.pop("code")


@pytest.mark.parametrize("bad", [1, 0, "true", None, []])
def test_unknown_must_be_a_real_bool(bad):
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.reason("ai_excluded", "ai_flag", True, unknown=bad)
    assert caught.value.code == "reason_unknown_not_bool"


def test_a_code_outside_the_table_is_refused():
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.reason("too_small", "bookmarks", 1)
    assert caught.value.code == "reason_code_unknown"


def test_a_field_outside_the_table_is_refused():
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.reason("favorites_below", "likes", 1)
    assert caught.value.code == "reason_field_unknown"


@pytest.mark.parametrize("value", [None, 0, -3, "12", True, False])
def test_evidence_accepts_json_scalars_without_coercion(value):
    record = fr.reason("field_unknown", "bookmarks", value, unknown=True)
    assert record["evidence"] is value
    assert type(record["evidence"]) is type(value)


def test_a_bool_evidence_stays_a_bool_not_a_number():
    record = fr.reason("ai_excluded", "ai_flag", True)
    assert record["evidence"] is True
    assert type(record["evidence"]) is bool


@pytest.mark.parametrize("value", [[1], {"a": 1}, (1,), b"x", {1, 2}, 3.5, 2.0])
def test_non_scalar_evidence_is_refused(value):
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.reason("field_unknown", "bookmarks", value, unknown=True)
    assert caught.value.code == "reason_evidence_not_scalar"


def test_long_text_is_truncated_and_marked():
    long_text = "x" * (fr.EVIDENCE_MAX_CHARS + 50)
    record = fr.reason("field_unknown", "original_urls", long_text, unknown=True)
    assert record["evidence"].endswith(fr.TRUNCATION_MARKER)
    assert record["evidence"] == "x" * fr.EVIDENCE_MAX_CHARS + fr.TRUNCATION_MARKER
    assert len(record["evidence"]) == fr.MAX_EVIDENCE_CHARS
    fr.validate_reason(record)  # the truncated value must itself be legal


def test_validate_refuses_evidence_longer_than_a_record_may_hold():
    handmade = {
        "code": "field_unknown",
        "field": "original_urls",
        "evidence": "y" * (fr.MAX_EVIDENCE_CHARS + 1),
        "unknown": True,
    }
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.validate_reason(handmade)
    assert caught.value.code == "reason_evidence_too_long"


def test_validate_requires_exactly_the_four_keys():
    base = {"code": "ai_excluded", "field": "ai_flag", "evidence": True, "unknown": False}
    fr.validate_reason(base)
    for broken in (
        {key: value for key, value in base.items() if key != "code"},
        dict(base, extra=1),
    ):
        with pytest.raises(fr.FilterReasonError) as caught:
            fr.validate_reason(broken)
        assert caught.value.code == "reason_shape_invalid"


def test_validate_accepts_a_plain_mapping_and_rejects_a_non_mapping():
    fr.validate_reason(
        {"code": "ai_excluded", "field": "ai_flag", "evidence": True, "unknown": False}
    )
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.validate_reason(["not", "a", "mapping"])
    assert caught.value.code == "reason_shape_invalid"


def test_the_legacy_map_covers_all_eight_codes_and_passed_the_import_check():
    assert set(fr.LEGACY_CODE_MAP) == set(fr.LEGACY_CODES)
    assert len(fr.LEGACY_CODES) == 8
    fr.check_legacy_map()
    assert fr._LEGACY_CHECKED is True
    for legacy, (code, field, unknown) in fr.LEGACY_CODE_MAP.items():
        assert code in fr.REASON_CODES, legacy
        assert field in fr.FIELDS, legacy
        assert type(unknown) is bool, legacy


def test_the_legacy_check_catches_missing_extra_and_bad_targets():
    complete = dict(fr.LEGACY_CODE_MAP)
    missing = dict(complete)
    missing.pop("ai_excluded")
    extra = dict(complete, unknown_code=("ai_excluded", "ai_flag", False))
    bad_code = dict(complete, ai_excluded=("not_a_code", "ai_flag", False))
    bad_field = dict(complete, ai_excluded=("ai_excluded", "not_a_field", False))
    bad_flag = dict(complete, ai_excluded=("ai_excluded", "ai_flag", "false"))
    bad_shape = dict(complete, ai_excluded=("ai_excluded", "ai_flag"))
    for table, expected in (
        (missing, "legacy_map_incomplete"),
        (extra, "legacy_map_extra"),
        (bad_code, "legacy_map_code_unknown"),
        (bad_field, "legacy_map_field_unknown"),
        (bad_flag, "legacy_map_unknown_not_bool"),
        (bad_shape, "legacy_map_shape"),
    ):
        with pytest.raises(fr.FilterReasonError) as caught:
            fr.check_legacy_map(table)
        assert caught.value.code == expected


def test_every_legacy_code_translates_into_a_legal_record():
    for legacy in fr.LEGACY_CODES:
        record = fr.translate(legacy)
        fr.validate_reason(record)
        assert record["evidence"] == legacy
        assert record["unknown"] is fr.LEGACY_CODE_MAP[legacy][2]
    with pytest.raises(fr.FilterReasonError) as caught:
        fr.translate("something_new")
    assert caught.value.code == "legacy_code_unknown"


def test_verdict_prefers_a_known_rejection_over_an_unknown():
    known = fr.reason("favorites_below", "bookmarks", 3)
    unknown = fr.reason("field_unknown", "ai_flag", None, unknown=True)
    assert fr.verdict_of([]) == "match"
    assert fr.verdict_of([known]) == "exclude"
    assert fr.verdict_of([unknown]) == "unknown"
    assert fr.verdict_of([unknown, known]) == "exclude"
    assert fr.verdict_of([fr.reason("ai_excluded", "ai_flag", True)]) == "exclude"
    other_unknown = fr.reason("field_unknown", "bookmarks", None, unknown=True)
    assert fr.verdict_of([unknown, other_unknown]) == "unknown"


def test_verdict_validates_every_record_it_is_given():
    with pytest.raises(fr.FilterReasonError):
        fr.verdict_of([{"code": "made_up", "field": "bookmarks", "evidence": 1, "unknown": False}])
    with pytest.raises(fr.FilterReasonError):
        fr.verdict_of(["not a record"])


def test_the_error_is_a_value_error_with_a_code():
    error = fr.FilterReasonError("reason_code_unknown", "x")
    assert isinstance(error, ValueError)
    assert error.code == "reason_code_unknown"
    assert "x" in str(error)
