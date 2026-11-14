"""An immutable download scope derived from one follow snapshot's memberships.

A scope is a *member set*, not a list of group names.  The website can rename a
group without touching its members, and the same artist can sit in several
groups; both facts have to stop at this boundary, or "download this group"
would quietly change what it means between two runs.

Nothing here reads a database, the gallery or the network: a snapshot is passed
in, and the result is data.  The scope carries a fingerprint of its member set so
a later snapshot can be compared with it (ensure_current), which is the only
defence against a scope being widened behind a running task's back.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType

from organizer.common import OrganizerError, identity, text

KINDS = ("group_download",)
COMPLETENESS = ("complete", "partial", "unknown")


def _fingerprint(artist_ids):
    """A digest of the member set, independent of order and of group names.

    Group names are deliberately outside the digest: a rename must not look
    like a different scope, because the members did not move.
    """
    payload = json.dumps(
        {"artist_ids": list(artist_ids)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _requested_groups(groups):
    if isinstance(groups, (str, bytes)) or not isinstance(groups, (list, tuple)):
        raise OrganizerError("scope_groups_invalid")
    names = [text(name, 20) for name in groups]
    if not names:
        raise OrganizerError("scope_requires_group")
    if len(set(names)) != len(names):
        raise OrganizerError("scope_group_duplicate")
    return tuple(sorted(names))


def _membership_names(snapshot):
    """Every group name this snapshot knows about, from the widest source."""
    known = set()
    partitions = snapshot.get("memberships")
    if isinstance(partitions, Mapping):
        for side in ("show", "hide"):
            side_map = partitions.get(side)
            if isinstance(side_map, Mapping):
                known.update(str(name) for name in side_map)
    for artist_id, row in snapshot["artists"].items():
        if not isinstance(row, Mapping):
            # A row that is not a mapping is a broken snapshot, not an
            # artist without groups: say so before asking about group names.
            raise OrganizerError("snapshot_artist_invalid")
        for name in row.get("groups") or ():
            known.add(str(name))
            if not str(name):
                raise OrganizerError("snapshot_artist_invalid")
    return known


def _visibility_of(rows):
    seen = {str(row.get("visibility")) for row in rows if isinstance(row, Mapping)}
    if seen == {"0"}:
        return "show"
    if seen == {"1"}:
        return "hide"
    if not seen:
        return "none"
    return "mixed"


def _completeness(snapshot):
    """What this snapshot can honestly say about being the whole membership."""
    if "memberships_complete" not in snapshot:
        return "unknown", "memberships_not_recorded"
    if not snapshot["memberships_complete"]:
        return "partial", "memberships_incomplete"
    status = snapshot.get("status")
    if status not in (None, "complete"):
        return "partial", "snapshot_status_%s" % (status,)
    return "complete", ""


class GroupScope:
    """One immutable member set plus the evidence it was derived from."""

    def __init__(self, *, kind, snapshot_id, groups, artist_ids, overlap, source,
                 completeness, unknown_reason):
        if kind not in KINDS:
            raise OrganizerError("scope_kind_invalid")
        if completeness not in COMPLETENESS:
            raise OrganizerError("scope_completeness_invalid")
        self.kind = kind
        self.snapshot_id = snapshot_id
        self.groups = tuple(groups)
        self.artist_ids = tuple(artist_ids)
        self.overlap = MappingProxyType({key: tuple(value) for key, value in overlap.items()})
        self.source = MappingProxyType(dict(source))
        self.completeness = completeness
        self.unknown_reason = unknown_reason
        self.fingerprint = _fingerprint(self.artist_ids)

    @property
    def complete(self):
        """True only when the snapshot can prove it saw every membership."""
        return self.completeness == "complete"

    @property
    def complete_reason(self):
        return self.unknown_reason

    def as_dict(self):
        return {
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "groups": list(self.groups),
            "artist_ids": list(self.artist_ids),
            "overlap": {key: list(value) for key, value in self.overlap.items()},
            "source": dict(self.source),
            "completeness": self.completeness,
            "unknown_reason": self.unknown_reason,
            "fingerprint": self.fingerprint,
        }

    def __eq__(self, other):
        if not isinstance(other, GroupScope):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self):  # pragma: no cover - debugging aid
        return "GroupScope(%s, %d artists, %s)" % (
            self.groups, len(self.artist_ids), self.completeness)


def build_scope(snapshot, *, groups, kind="group_download"):
    """Derive the member set of the requested groups from one snapshot.

    Raises OrganizerError with a stable code when the input cannot answer: a
    missing snapshot, missing artists, or a group the snapshot does not know.
    An empty member set is a legitimate answer and is not an error.
    """
    if kind not in KINDS:
        raise OrganizerError("scope_kind_invalid")
    if not isinstance(snapshot, Mapping):
        raise OrganizerError("snapshot_not_a_mapping")
    artists = snapshot.get("artists")
    if not isinstance(artists, Mapping):
        raise OrganizerError("snapshot_artists_missing")
    wanted = _requested_groups(groups)
    known = _membership_names(snapshot)
    missing = [name for name in wanted if name not in known]
    if missing:
        raise OrganizerError("group_not_found")

    members = {}
    hit_rows = []
    for artist_id, row in artists.items():
        if not isinstance(row, Mapping):
            raise OrganizerError("snapshot_artist_invalid")
        artist = identity(artist_id)
        if row.get("is_following") is False:
            continue
        names = [str(name) for name in row.get("groups") or ()]
        hit = [name for name in names if name in wanted]
        if not hit:
            continue
        members[artist] = tuple(sorted(hit))
        hit_rows.append(row)

    completeness, reason = _completeness(snapshot)
    ordered = tuple(sorted(members))
    overlap = {key: value for key, value in members.items() if len(value) > 1}
    return GroupScope(
        kind=kind,
        snapshot_id=str(snapshot.get("id") or ""),
        groups=wanted,
        artist_ids=ordered,
        overlap=overlap,
        source={"visibility": _visibility_of(hit_rows),
                "catalog_complete": completeness == "complete"},
        completeness=completeness,
        unknown_reason=reason,
    )


def ensure_current(scope, snapshot, *, groups=None):
    """Rebuild the scope and refuse when its member set has moved.

    A widened or narrowed member set is not something a running task may
    absorb: the caller has to create a new task, so this raises scope_changed
    instead of returning the fresher scope.

    `groups` exists because a rename moves no artist but does move the name:
    the conflict pass (which sees both snapshots) tells this function which
    name the group carries now, and the member set still has to match.  With
    the default the stored names are used, so a group that disappeared from
    the snapshot is reported as group_not_found rather than as a change.
    """
    if not isinstance(scope, GroupScope):
        raise OrganizerError("scope_required")
    wanted = list(scope.groups) if groups is None else list(groups)
    fresh = build_scope(snapshot, groups=wanted, kind=scope.kind)
    if fresh.fingerprint != scope.fingerprint:
        raise OrganizerError("scope_changed")
    return fresh
