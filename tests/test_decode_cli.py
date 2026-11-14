"""The decode-check command: exit codes and words that match the facts."""
from __future__ import annotations

import json

import png_samples as samples
from core.library_maintenance import main


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_valid_png_exits_zero_and_says_the_bytes_stayed(tmp_path, capsys):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["decode-check", str(path)])
    printed = capsys.readouterr().out
    assert code == 0
    assert "解码: ok" in printed
    assert "字节未变: 是" in printed


def test_structure_only_never_claims_a_decode(tmp_path, capsys):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["decode-check", str(path), "--structure-only"])
    printed = capsys.readouterr().out
    assert code == 5
    assert "解码: undecided（not_requested）" in printed
    assert "解码: ok" not in printed


def test_a_corrupt_file_exits_two_not_five(tmp_path, capsys):
    path = _write(tmp_path, "bad_crc.png", samples.build_bad_crc())
    code = main(["decode-check", str(path)])
    printed = capsys.readouterr().out
    assert code == 2
    assert "结构: failed" in printed


def test_a_format_without_a_decoder_exits_five_and_names_the_reason(tmp_path, capsys):
    path = _write(tmp_path, "tiny.gif", samples.build_minimal_gif())
    code = main(["decode-check", str(path)])
    printed = capsys.readouterr().out
    assert code == 5
    assert "解码: capability_unavailable（no_decoder_component）" in printed


def test_json_mode_is_one_object_with_the_exit_code(tmp_path, capsys):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["--json", "decode-check", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exit_code"] == 0
    assert payload["decode_policy"] == "strict"
    assert payload["files"][0]["unchanged"] is True


def test_a_missing_file_is_three_not_a_traceback(tmp_path, capsys):
    code = main(["--json", "decode-check", str(tmp_path / "nope.png")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["status"] == "error"


def test_a_zero_limit_is_refused_instead_of_silently_replaced(tmp_path, capsys):
    """--max-bytes 0 used to fall back to 256 MiB through an "x or default".

    A limit that cannot be honoured is a rejected argument: exit 4, no
    traceback, no quiet substitution.
    """
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["decode-check", str(path), "--max-bytes", "0"])
    captured = capsys.readouterr()
    assert code == 4
    assert "参数被拒" in captured.err
    assert "Traceback" not in captured.err


def test_a_negative_limit_is_a_refusal_with_the_json_shape(tmp_path, capsys):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["--json", "decode-check", str(path), "--max-pixels", "-1"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["status"] == "error"
    assert payload["code"] == "invalid_argument"
    assert "max_pixels" in payload["detail"]


def test_an_explicit_limit_that_can_be_honoured_still_runs(tmp_path, capsys):
    path = _write(tmp_path, "valid.png", samples.build_valid_rgba())
    code = main(["decode-check", str(path), "--max-bytes", "1000000"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "解码: ok" in printed
