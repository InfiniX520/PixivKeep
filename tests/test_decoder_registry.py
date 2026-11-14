"""The capability gate: no decoder means no `ok`.

These tests never install anything and never open a real image.  Where a decoder
is needed they inject a fake one, because the point of the gate is what happens
when the component is absent -- which is the honest state of this build.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import decoder_registry as registry


class FakeDecoder:
    """A decoder that answers exactly what the test tells it to."""

    def __init__(self, result=None, *, error=None, name="fake", formats=("png",)):
        self.name = name
        self.formats = formats
        self._result = result
        self._error = error
        self.calls = []

    def decode(self, path, *, limits, stop):
        self.calls.append((path, limits, stop))
        if self._error is not None:
            raise self._error
        return self._result


def test_without_a_decoder_a_strict_policy_is_capability_unavailable():
    outcome = registry.decode_with(None, "whatever.png", policy="strict")
    assert outcome.decode == registry.DECODE_UNAVAILABLE
    assert outcome.reason == registry.NO_DECODER
    assert outcome.decoder is None


def test_without_a_decoder_the_structure_policy_says_structure_only():
    outcome = registry.decode_with(None, "whatever.png", policy="structure")
    assert outcome.decode == registry.DECODE_UNAVAILABLE
    assert outcome.reason == registry.STRUCTURE_ONLY


def test_the_public_probe_reports_no_component():
    assert registry.probe_decoder() is None
    for token in ("png", "PNG", ".png", "image/png", "jpeg", "jpg", "gif", "webp"):
        assert registry.capability(token) == "unavailable"
    assert registry.capability("bmp") == "unavailable"
    assert registry.capability(None) == "unavailable"
    assert registry.capability(123) == "unavailable"


def test_an_injected_decoder_makes_exactly_its_own_formats_available():
    fake = FakeDecoder(formats=("png",))
    for token in ("png", "PNG", ".png", "image/png"):
        assert registry.capability(token, decoder=fake, injected=True) == "available"
    for token in ("jpeg", "jpg", "gif", "webp", "bmp"):
        assert registry.capability(token, decoder=fake, injected=True) == "unavailable"


def test_media_type_aliases_are_normalised_on_both_sides():
    fake = FakeDecoder(formats=("image/jpeg",))
    assert registry.capability("jpg", decoder=fake, injected=True) == "available"
    assert registry.capability("image/jpeg", decoder=fake, injected=True) == "available"


def test_a_decoder_that_says_ok_is_passed_through_with_its_name():
    fake = FakeDecoder({"decode": "ok", "reason": None}, name="pillow-like")
    outcome = registry.decode_with(fake, "a.png", policy="strict", limits="L", stop="S")
    assert outcome.decode == registry.DECODE_OK
    assert outcome.decoder == "pillow-like"
    assert fake.calls == [("a.png", "L", "S")]


def test_a_decoder_that_raises_becomes_failed_and_names_the_error():
    fake = FakeDecoder(error=RuntimeError("boom"))
    outcome = registry.decode_with(fake, "a.png", policy="strict")
    assert outcome.decode == registry.DECODE_FAILED
    assert outcome.reason.startswith("decoder_error:RuntimeError")


def test_a_decoder_that_declines_is_undecided_not_failed():
    fake = FakeDecoder({"decode": "undecided", "reason": "pixels_limit"})
    outcome = registry.decode_with(fake, "a.png", policy="strict")
    assert outcome.decode == registry.DECODE_UNDECIDED
    assert outcome.reason == "pixels_limit"


@pytest.mark.parametrize(
    "result",
    [{"weird": True}, "ok", None, {"decode": "capability_unavailable"}],
)
def test_a_result_this_module_cannot_read_is_never_ok(result):
    fake = FakeDecoder(result)
    outcome = registry.decode_with(fake, "a.png", policy="strict")
    assert outcome.decode == registry.DECODE_FAILED
    assert outcome.reason == registry.RESULT_INVALID


def test_an_unknown_policy_is_a_programmer_error():
    with pytest.raises(ValueError):
        registry.decode_with(None, "a.png", policy="whatever")


def test_an_interrupt_keeps_its_own_type():
    fake = FakeDecoder(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        registry.decode_with(fake, "a.png", policy="strict")


def test_the_module_cannot_install_or_import_an_external_decoder():
    source = Path(registry.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {"__future__", "dataclasses", "typing", "collections", "enum"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= allowed, "unexpected import(s): %s" % sorted(imported - allowed)
    forbidden_calls = {"install", "uninstall", "upgrade", "check_call", "run"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            assert name not in forbidden_calls, "the gate must not run subprocesses"
