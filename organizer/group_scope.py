"""An immutable download scope derived from one follow snapshot's memberships.

A scope is a *member set*, not a list of group names.  Three facts have to stop
at this boundary, or "download this group" would quietly mean something else
between two runs:

* the website can rename a group without touching its members;
* the same artist can sit in several groups;
* public and private follows are two separate namespaces, so two groups may
  share a name and still be different groups.

A snapshot carries two descriptions of the same membership: the memberships
block and each artist's group list.  Both are read, and when they disagree the
member set is not called complete (audit EA-08).  The resulting scope is
immutable from the outside: its attributes cannot be assigned to, and its
exports are fresh copies.

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

#: How much a completeness answer proves, so degradation can be detected.
_EVIDENCE_RANK = {"unknown": 0, "partial": 1, "complete": 2}


class ScopeFieldError(OrganizerError):
    """A field of a scope dump is missing, or has a shape that cannot answer.

    The base class washes any code that is not a plain identifier, and a code
    that names its field carries a subject (scope_field_invalid:blocks).  This
    subclass keeps the subject, so a refusal says which field was wrong instead
    of organizer_error - the same pattern AmbiguousGroupError uses for a group
    name.
    """

    def __init__(self, code, field):
        super().__init__(code)
        self.code = "%s:%s" % (code, field)
        self.field = str(field)


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


def _names(items, limit=3):
    """A short, stable list for a reason code: first few names, then a count."""
    head = ",".join(items[:limit])
    return head if len(items) <= limit else "%s,+%d" % (head, len(items) - limit)


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


#: A group the memberships block names but whose member list it never records.
_NOT_RECORDED = object()


def _membership_rows(snapshot):
    """{side: {name: set-of-ids | None}} from the snapshot's memberships block.

    None means "the block lists this group but not its members"; that is not an
    empty group, it is a group whose contents the snapshot never recorded.  The
    block is optional: a snapshot that never carried it yields None, and the
    caller must then say so instead of pretending to have cross-checked it.
    """
    block = snapshot.get("memberships")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise OrganizerError("snapshot_membership_invalid")
    recorded = {}
    for side in VISIBILITY:
        side_map = block.get(side)
        if side_map is None:
            continue
        if not isinstance(side_map, Mapping):
            raise OrganizerError("snapshot_membership_invalid")
        groups = {}
        for name, entry in side_map.items():
            if entry is None:
                groups[str(name)] = None
                continue
            if not isinstance(entry, Mapping):
                raise OrganizerError("snapshot_membership_invalid")
            ids = entry.get("ids")
            if ids is None:
                groups[str(name)] = None
                continue
            if isinstance(ids, (str, bytes)) or not isinstance(ids, (list, tuple)):
                raise OrganizerError("snapshot_membership_invalid")
            members = set()
            for item in ids:
                # An id is a scalar; a nested object here means the block was
                # not written by the collector and cannot be read as a member.
                if type(item) not in (str, int):
                    raise OrganizerError("snapshot_membership_invalid")
                members.add(str(item))
            groups[str(name)] = members
        recorded[side] = groups
    return recorded


def _cross_check(wanted, rows_by_pair, recorded):
    """Compare each requested group's recorded ids with the artist rows.

    Two independent facts describe the same group: the memberships block and
    the per-artist group lists.  If they disagree the snapshot contradicts
    itself and its member set cannot be called complete, whatever the top-level
    boolean says (audit EA-08).  The check is only possible where both facts
    exist; a pair the block never enumerated is reported, not assumed empty.
    """
    if recorded is None:
        return [], []
    mismatches = []
    unknown = []
    for side, name in wanted:
        ids = (recorded.get(side) or {}).get(name, _NOT_RECORDED)
        if ids is _NOT_RECORDED or ids is None:
            unknown.append("%s:%s" % (side, name))
            continue
        if ids != rows_by_pair.get((side, name), set()):
            mismatches.append("%s:%s" % (side, name))
    return mismatches, unknown


class GroupScope:
    """One immutable member set plus the evidence it was derived from.

    Every attribute is read-only: values are served from one private mapping
    and any assignment raises AttributeError.  A scope is a checkpoint, and a
    checkpoint whose member set can be reassigned afterwards is not one (audit
    EA-06).

    The fingerprint is a digest of the member set, not a signature: it detects
    drift between two copies of the same scope, it does not stop anyone from
    recomputing it on purpose, and the documentation must not claim otherwise.
    """

    __slots__ = ("_values",)

    def __init__(self, *, kind, snapshot_id, group_pairs, artist_ids, sides,
                 blocks, overlap, source, completeness, unknown_reason):
        if kind not in KINDS:
            raise OrganizerError("scope_kind_invalid")
        if completeness not in COMPLETENESS:
            raise OrganizerError("scope_completeness_invalid")
        pairs = tuple((str(side), str(name)) for side, name in group_pairs)
        for side, _name in pairs:
            if side not in VISIBILITY:
                raise OrganizerError("scope_group_invalid")
        sides = tuple(str(side) for side in sides)
        for side in sides:
            if side not in VISIBILITY:
                raise OrganizerError("scope_group_invalid")
        blocks = tuple((str(side), tuple(str(item) for item in ids))
                       for side, ids in blocks)
        for side, _ids in blocks:
            if side not in VISIBILITY:
                raise OrganizerError("scope_group_invalid")
        members = tuple(str(item) for item in artist_ids)
        # The flat member list and the per-side blocks describe one set, or
        # this was never a scope: a dump with one of them edited must not
        # become an object that claims both.
        from_blocks = tuple(sorted({artist for _side, ids in blocks for artist in ids}))
        if tuple(sorted(members)) != from_blocks:
            raise OrganizerError("scope_members_inconsistent")
        values = {
            "kind": kind,
            "snapshot_id": str(snapshot_id),
            "group_pairs": pairs,
            "groups": tuple(sorted({name for _side, name in pairs})),
            "artist_ids": members,
            # The identity inputs are kept so as_dict()/from_dict() can carry
            # them: a round trip that dropped them could not re-derive the
            # fingerprint and would have to trust whatever the file claimed.
            "sides": sides,
            "blocks": blocks,
            "overlap": MappingProxyType({str(key): tuple(value)
                                         for key, value in overlap.items()}),
            "source": MappingProxyType({str(key): value
                                        for key, value in source.items()}),
            "completeness": completeness,
            "unknown_reason": str(unknown_reason),
            "fingerprint": _fingerprint(sides, blocks),
        }
        object.__setattr__(self, "_values", MappingProxyType(values))

    def __getattr__(self, name):
        # Only reached for names the class itself does not define, so the
        # properties below and the methods keep working unchanged.
        try:
            return object.__getattribute__(self, "_values")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        raise AttributeError("GroupScope is immutable: %s" % (name,))

    def __delattr__(self, name):
        raise AttributeError("GroupScope is immutable: %s" % (name,))

    def __copy__(self):
        return GroupScope.from_dict(self.as_dict())

    def __deepcopy__(self, memo):
        return GroupScope.from_dict(self.as_dict())

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
            raise ScopeFieldError("scope_field_missing", ",".join(missing))

        def items_of(raw, what):
            """A list of entries, or a typed refusal that names the field."""
            if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
                raise ScopeFieldError("scope_field_invalid", what)
            return raw

        pairs = []
        for item in items_of(value["group_pairs"], "group_pairs"):
            if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ScopeFieldError("scope_field_invalid", "group_pairs")
            pairs.append((str(item[0]), str(item[1])))
        blocks = []
        for item in items_of(value["blocks"], "blocks"):
            if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ScopeFieldError("scope_field_invalid", "blocks")
            blocks.append((str(item[0]),
                           tuple(str(entry) for entry in items_of(item[1], "blocks"))))
        if not isinstance(value["overlap"], Mapping):
            raise ScopeFieldError("scope_field_invalid", "overlap")
        if not isinstance(value["source"], Mapping):
            raise ScopeFieldError("scope_field_invalid", "source")
        scope = cls(
            kind=value["kind"],
            snapshot_id=value["snapshot_id"],
            group_pairs=pairs,
            artist_ids=tuple(str(item) for item in items_of(value["artist_ids"], "artist_ids")),
            sides=tuple(items_of(value["sides"], "sides")),
            blocks=blocks,
            overlap={str(key): tuple(ids) for key, ids in value["overlap"].items()},
            source={str(key): item for key, item in value["source"].items()},
            completeness=value["completeness"],
            unknown_reason=value["unknown_reason"],
        )
        claimed = value.get("fingerprint")
        if claimed is not None and claimed != scope.fingerprint:
            raise OrganizerError("scope_fingerprint_mismatch")
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
    # What the artist rows say each requested group contains, including rows
    # that are no longer followed: the memberships block lists members, and a
    # member who is not in the rows is exactly the disagreement being looked
    # for.
    rows_by_pair = {}
    for artist_id, row in artists.items():
        if not isinstance(row, Mapping):
            raise OrganizerError("snapshot_artist_invalid")
        artist = identity(artist_id)
        following = row.get("is_following") is not False
        side = _side_of(row)
        if side is None:
            if following:
                raise OrganizerError("snapshot_artist_invalid")
            continue
        names = [str(name) for name in row.get("groups") or ()]
        for name in names:
            pair = (side, name)
            if pair in wanted:
                rows_by_pair.setdefault(pair, set()).add(artist)
        if not following:
            continue
        hit = [name for pair_side, name in wanted if pair_side == side and name in names]
        if not hit:
            continue
        members[artist] = tuple(sorted(set(hit)))
        by_side.setdefault(side, []).append(artist)
        hit_rows.append(row)

    completeness, reason = _completeness(snapshot)
    recorded = _membership_rows(snapshot)
    mismatches, unrecorded = _cross_check(wanted, rows_by_pair, recorded)
    if mismatches and completeness == "complete":
        # Two facts about the same group disagree, so the member set cannot be
        # called whole - whatever the top-level boolean says (audit EA-08).
        completeness = "partial"
        reason = "scope_membership_mismatch:" + _names(mismatches)
    if recorded is None:
        membership_evidence = "not_recorded"
    elif unrecorded:
        membership_evidence = "partially_recorded"
    else:
        membership_evidence = "cross_checked"
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
                "catalog_complete": completeness == "complete",
                "memberships": membership_evidence,
                "membership_mismatches": list(mismatches)},
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

    The same members are not the same evidence: a snapshot whose memberships
    block now contradicts the artist rows proves less than the one the task
    started with, so a lower completeness is its own refusal
    (scope_evidence_degraded).  More evidence is not a refusal: it is returned,
    because the caller may then rely on it.
    """
    if not isinstance(scope, GroupScope):
        raise OrganizerError("scope_required")
    wanted = list(scope.group_pairs) if groups is None else list(groups)
    fresh = build_scope(snapshot, groups=wanted, kind=scope.kind)
    if fresh.fingerprint != scope.fingerprint:
        raise OrganizerError("scope_changed")
    if _EVIDENCE_RANK[fresh.completeness] < _EVIDENCE_RANK[scope.completeness]:
        # Same members, weaker proof.  A resumed task must not keep trusting a
        # completeness the current snapshot no longer supports.
        raise OrganizerError("scope_evidence_degraded")
    return fresh
