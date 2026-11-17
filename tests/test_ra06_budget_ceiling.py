"""One check may never read past the byte ceiling, whatever the decoder does.

The decode reads through a pass that hashes what it takes, and the pass then
takes the rest of the file so the digest covers the whole version.  The decoder's
own bytes therefore have to be charged before that second read happens: charging
them afterwards lets the meter believe a whole ceiling is still available and one
check reads (and hashes) up to twice the ceiling.  Today's PNG decoder reads to
EOF, so this file is the guard for a streaming decoder, not a live bug.
"""
from __future__ import annotations

from core import quality_check as q
from core.decode_limits import DecodeLimits
from core.quality_evidence import DecodeReceipt


class _Structure:
    media_type = "image/png"
    sha256 = None


def test_a_decoder_that_stops_early_cannot_double_the_read(tmp_path, monkeypatch):
    target = tmp_path / "image.png"
    target.write_bytes(b"x" * 200)
    # The ceiling has to admit the file itself (200), leave room for the version
    # hash (200 more is not possible), so it is set to 300: one whole hash plus
    # 100 bytes.  A decoder that reads those 100 and stops must not be followed
    # by a second read of the other 100.
    limits = DecodeLimits(max_bytes=300)

    monkeypatch.setattr(
        q, "_structure", lambda *args, **kwargs: (_Structure(), q.Dimension("ok"))
    )

    def streaming_decode(path, media_type, structure_state, *, policy, limits, stop,
                         stream=None):
        # A decoder that judges the head of the file and stops: the pass is left
        # with 100 bytes it has read, and the file has 100 more.
        head = stream.read(100)
        return q.Dimension("ok"), DecodeReceipt(
            format="png",
            frames=1,
            pixels=1,
            limits=limits.as_dict(),
            decoder="fake-streaming",
            outcome="ok",
            seconds=0.0,
            checked_at="2026-01-01T00:00:00Z",
            bytes_read=len(head),
        )

    monkeypatch.setattr(q, "_decode", streaming_decode)

    report = q.check_file(target, decode_policy="strict", limits=limits)

    # The version hash took 200 of the 300 bytes, the decoder took the other 100;
    # there is nothing left, so the pass must stop short instead of reading the
    # file a second time.  Measured: the previous order finished the whole file
    # and reported 400 bytes spent against a 300 byte ceiling, then certified that
    # content as observed.
    assert report["content"]["complete"] is False
    assert report["content"]["state"] != "observed"
    assert report["quality"]["decode"]["state"] != "ok"
    assert int(report["budget"]["bytes"]) <= 300
