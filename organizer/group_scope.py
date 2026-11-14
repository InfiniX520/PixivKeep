"""An immutable download scope derived from one follow snapshot's memberships.

A scope is a *member set*, not a list of group names.  Three facts have to stop
at this boundary, or "download this group" would quietly mean something else
between two runs:

* the website can rename a group without touching its members;
* the same artist can sit in several groups;
* public and private follows are two separate namespaces, so two groups may
  share a name and still be different groups.

Nothing here reads a database, the gallery or the network: a snapshot is passed
in, and the result is data.  The scope carries a fingerprint of its member set
per visibility, so a later snapshot can be compared with it (ensure_current),
which is the only defence against a scope being widened behind a running task.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType

from organizer.common import OrganizerError, identity, text

KINDS = ("group_download",)
COMPLETENESS = ("complete", "partial", "unknown")
VISIBILITY = ("show", "hide")

#: The snapshot keeps the website flag ("0" public, "1" private).
_SIDE_OF_FLAG = {"0": "show", "1": "hide"}


class AmbiguousGroupError(OrganizerError):
    """A bare group name that exists on both sides cannot be resolved.

    The code keeps the name so a caller can say which group was ambiguous,
    while the class stays an OrganizerError so existing catches keep working.
    """

    def __init__(self, name):
        super().__init__("group_ambiguous")
        self.group = name
        self.code = "group_ambiguous:%s" % (name,)


def _fingerprint(sides, blocks):
    """A digest of the member set per visibility, independent of order.

    Group *names* stay outside the digest: a rename moves no member, so it must
    not look like a different scope.  The sides are inside it: the same name on
    the other side is a different group even when both happen to be empty.
    """
    payload = json.dumps(
        {
            "sides": list(sides),
            "members": [
                {"visibility": side, "artist_ids": list(ids)} for side, ids in blocks
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _side_of(row):
    return _SIDE_OF_FLAG.get(str(row.get("visibility")))


def _known_pairs(snapshot):
    """Every (visibility, name) pair this snapshot knows about."""
    pairs = set()
    partitions = snapshot.get("memberships")
    if isinstance(partitions, Mapping):
        for side in VISIBILITY:
            side_map = partitions.get(side)
            if isinstance(side_map, Mapping):
                for name in side_map:
                    pairs.add((side, str(name)))
    for artist_id, row in snapshot["artists"].items():
        if not isinstance(row, Mapping):
            # A row that is not a mapping is a broken snapshot, not an artist
            # without groups: say so before asking about group names.
            raise OrganizerError("snapshot_artist_invalid")
        side = _side_of(row)
        for name in row.get("groups") or ():
            name = str(name)
            if not name:
                raise OrganizerError("snapshot_artist_invalid")
            if side is not None:
                pairs.add((side, name))
    return pairs


def _plain_name(name, known):
    """A bare name is only usable when exactly one group carries it."""
    name = text(name, 20)
    hits = sorted(pair for pair in known if pair[1] == name)
    if not hits:
        raise OrganizerError("group_not_found")
    if len(hits) > 1:
        raise AmbiguousGroupError(name)
    return hits[0]


def _pair(value, known):
    side, name = value[0], text(value[1], 20)
    if side not in VISIBILITY:
        raise OrganizerError("scope_group_invalid")
    pair = (side, name)
    if pair not in known:
        raise OrganizerError("group_not_found")
    return pair


def _requested_groups(groups, known):
    if isinstance(groups, (str, bytes)) or not isinstance(groups, (list, tuple)):
        raise OrganizerError("scope_groups_invalid")
    if not groups:
        raise OrganizerError("scope_requires_group")
    resolved = []
    for item in groups:
        if isinstance(item, (tuple, list)):
            if len(item) != 2:
                raise OrganizerError("scope_group_invalid")
            resolved.append(_pair(item, known))
        else:
            resolved.append(_plain_name(item, known))
    if len(set(resolved)) != len(resolved):
        raise OrganizerError("scope_group_duplicate")
    return tuple(sorted(resolved))


def _visibility_of(rows):
    seen = {_side_of(row) for row in rows if isinstance(row, Mapping)}
    seen.discard(None)
    if seen == {"show"}:
        return "show"
    if seen == {"hide"}:
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

    def __init__(self, *, kind, snapshot_id, group_pairs, artist_ids, sides,
                 blocks, overlap, source, completeness, unknown_reason):
        if kind not in KINDS:
            raise OrganizerError("scope_kind_invalid")
        if completeness not in COMPLETENESS:
            raise OrganizerError("scope_completeness_invalid")
        self.kind = kind
        self.snapshot_id = snapshot_id
        self.group_pairs = tuple(group_pairs)
        self.groups = tuple(sorted({name for _, name in self.group_pairs}))
        self.artist_ids = tuple(artist_ids)
        self.overlap = MappingProxyType(
            {key: tuple(value) for key, value in overlap.items()}
        )
        self.source = MappingProxyType(dict(source))
        self.completeness = completeness
        self.unknown_reason = unknown_reason
        # The identity inputs are kept so as_dict()/from_dict() can carry them:
        # a round trip that dropped them could not re-derive the fingerprint and
        # would have to trust whatever the file claimed.
        self.sides = tuple(sides)
        self.blocks = tuple((str(side), tuple(ids)) for side, ids in blocks)
        self.fingerprint = _fingerprint(self.sides, self.blocks)

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
            "group_pairs": [list(pair) for pair in self.group_pairs],
            "artist_ids": list(self.artist_ids),
            "overlap": {key: list(value) for key, value in self.overlap.items()},
            "sides": list(self.sides),
            "blocks": [[side, list(ids)] for side, ids in self.blocks],
            "source": dict(self.source),
            "completeness": self.completeness,
            "unknown_reason": self.unknown_reason,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value):
        """Rebuild a scope from as_dict(), refusing a dump that does not match.

        The member blocks travel with the dump, so the fingerprint is re-derived
        here instead of trusted: a file whose members were edited after the fact
        fails with scope_fingerprint_mismatch rather than becoming a scope.
        """
        if not isinstance(value, Mapping):
            raise OrganizerError("scope_not_a_mapping")
        required = (
            "kind",
            "snapshot_id",
            "group_pairs",
            "artist_ids",
            "sides",
            "blocks",
            "overlap",
            "source",
            "completeness",
            "unknown_reason",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise OrganizerError("scope_field_missing:" + ",".join(missing))
        scope = cls(
            kind=value["kind"],
            snapshot_id=value["snapshot_id"],
            group_pairs=[tuple(pair) for pair in value["group_pairs"]],
            artist_ids=tuple(value["artist_ids"]),
            sides=tuple(value["sides"]),
            blocks=[(str(side), tuple(ids)) for side, ids in value["blocks"]],
            overlap={key: tuple(ids) for key, ids in dict(value["overlap"]).items()},
            source=dict(value["source"]),
            completeness=value["completeness"],
            unknown_reason=value["unknown_reason"],
        )
        claimed = value.get("fingerprint")
        if claimed is not None and claimed != scope.fingerprint:
            raise OrganizerError("scope_fingerprint_mismatch")
        # The fingerprint covers the sides and the member blocks, not the flat
        # artist list, so the two have to agree with each other as well: a dump
        # whose artist_ids were edited would otherwise pass on a valid
        # fingerprint while describing a different member set.
        from_blocks = tuple(sorted({artist for _, ids in scope.blocks for artist in ids}))
        if tuple(sorted(scope.artist_ids)) != from_blocks:
            raise OrganizerError("scope_members_inconsistent")
        return scope

    def __eq__(self, other):
        if not isinstance(other, GroupScope):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self):  # pragma: no cover - debugging aid
        return "GroupScope(%s, %d artists, %s)" % (
            self.group_pairs, len(self.artist_ids), self.completeness)


def build_scope(snapshot, *, groups, kind="group_download"):
    """Derive the member set of the requested groups from one snapshot.

    `groups` holds bare names (which must be unambiguous across both
    namespaces) or (visibility, name) pairs.  Raises OrganizerError with a
    stable code when the input cannot answer.  An empty member set is a
    legitimate answer and is not an error.
    """
    if kind not in KINDS:
        raise OrganizerError("scope_kind_invalid")
    if not isinstance(snapshot, Mapping):
        raise OrganizerError("snapshot_not_a_mapping")
    artists = snapshot.get("artists")
    if not isinstance(artists, Mapping):
        raise OrganizerError("snapshot_artists_missing")
    known = _known_pairs(snapshot)
    wanted = _requested_groups(groups, known)

    members = {}
    hit_rows = []
    by_side = {}
    for artist_id, row in artists.items():
        if not isinstance(row, Mapping):
            raise OrganizerError("snapshot_artist_invalid")
        artist = identity(artist_id)
        if row.get("is_following") is False:
            continue
        side = _side_of(row)
        if side is None:
            raise OrganizerError("snapshot_artist_invalid")
        names = [str(name) for name in row.get("groups") or ()]
        hit = [name for pair_side, name in wanted if pair_side == side and name in names]
        if not hit:
            continue
        members[artist] = tuple(sorted(set(hit)))
        by_side.setdefault(side, []).append(artist)
        hit_rows.append(row)

    completeness, reason = _completeness(snapshot)
    ordered = tuple(sorted(members))
    blocks = tuple((side, tuple(sorted(ids))) for side, ids in sorted(by_side.items()))
    overlap = {key: value for key, value in members.items() if len(value) > 1}
    sides = tuple(sorted({side for side, _ in wanted}))
    return GroupScope(
        kind=kind,
        snapshot_id=str(snapshot.get("id") or ""),
        group_pairs=wanted,
        artist_ids=ordered,
        sides=sides,
        blocks=blocks,
        overlap=overlap,
        source={"visibility": _visibility_of(hit_rows),
                "catalog_complete": completeness == "complete"},
        completeness=completeness,
        unknown_reason=reason,
    )


def ensure_current(scope, snapshot, *, groups=None):
    """Rebuild the scope and refuse when its member set has moved.

    A widened or narrowed member set is not something a running task may
    absorb: the caller has to create a new task, so this raises scope_changed.

    `groups` exists because a rename moves no artist but does move the name:
    the conflict pass (which sees both snapshots) tells this function which
    name the group carries now, and the member set still has to match.  With
    the default the stored pairs are used, so a group that disappeared from
    the snapshot is reported as group_not_found rather than as a change.
    """
    if not isinstance(scope, GroupScope):
        raise OrganizerError("scope_required")
    wanted = list(scope.group_pairs) if groups is None else list(groups)
    fresh = build_scope(snapshot, groups=wanted, kind=scope.kind)
    if fresh.fingerprint != scope.fingerprint:
        raise OrganizerError("scope_changed")
    return fresh
