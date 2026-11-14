"""What the website says now, versus what the last round planned to do.

The website is the authority.  A user may regroup an artist, rename a group or
delete one at any time, and this module's whole job is to notice that *before*
anything is written and to say "keep the website, ask a human" instead of
pushing an older plan over it.

Three properties are the point of the design:

* **it never emits a write.**  Every decision carries an action from a closed
  table, and none of those actions is "write".  `writes_for` returns [] for any
  input; the tests pin that, because "we only keep things" is easy to say and
  easy to lose in a later edit.
* **unknown is not "unchanged", and it is not "known bad" either.**  An artist
  the plan knows but the fresh snapshot does not is a *known* fact (artist_gone)
  and a human must look; only a picture that cannot be read at all is unknown.
  Treating a missing fact as "nothing changed" is how an old plan overwrites a
  manual edit.
* **a group is (visibility, name), never a bare name.**  The website lets the
  public and the private side carry the same name with different members, so a
  bare name that matches both sides is ambiguous and the answer is "ask a
  human", not "merge them" and not "pick one".

The snapshots and the plan are the shapes organizer/snapshots.py and
organizer/plans.py already persist (see E1_PLAN.md 3.4).  Nothing here opens a
file, a database or a socket; the caller passes plain dictionaries.
"""
from __future__ import annotations

KINDS = (
    "unchanged",
    "moved",
    "group_renamed",
    "group_deleted",
    "artist_gone",
    "new_member",
    "unknown",
)
ACTIONS = ("keep", "replan", "needs_human")

#: kind -> action.  A closed table, so a new kind cannot quietly default to
#: something optimistic: adding one here without an action fails the test.
ACTION_FOR = {
    "unchanged": "keep",
    "moved": "needs_human",
    "group_renamed": "needs_human",
    "group_deleted": "needs_human",
    "artist_gone": "needs_human",
    "new_member": "replan",
    "unknown": "needs_human",
}

#: Why a decision came out the way it did.  Closed on purpose: the report shows
#: these strings, so they have to be a vocabulary, not free text.
REASONS = (
    "same_as_planned",
    "website_groups_changed",
    "target_group_renamed",
    "target_group_deleted",
    "artist_not_followed_any_more",
    "artist_appeared_since_snapshot",
    "group_ambiguous",
    "snapshot_incomplete",
)

#: The two visibility partitions the snapshot stores; artists carry "0"/"1".
SIDES = ("show", "hide")
_SIDE_OF_CODE = {"0": "show", "1": "hide"}


def _side(value):
    """Normalise an artist's visibility to a partition name, or None."""
    text = str(value)
    if text in SIDES:
        return text
    return _SIDE_OF_CODE.get(text)


def _groups_of(snapshot, artist_id):
    """The group names one artist is in, from the snapshot's artist row."""
    artists = snapshot.get("artists") or {}
    row = artists.get(artist_id)
    if not isinstance(row, dict):
        return None
    groups = row.get("groups")
    if not isinstance(groups, (list, tuple)):
        return None
    return sorted({str(name) for name in groups})


def _directory(snapshot):
    """(side, name) -> set of member artist ids, from the membership partitions."""
    directory = {}
    partitions = snapshot.get("memberships") or {}
    if not isinstance(partitions, dict):
        return None
    for side in SIDES:
        side_partition = partitions.get(side) or {}
        if not isinstance(side_partition, dict):
            return None
        for tag, saved in side_partition.items():
            if not isinstance(saved, dict):
                return None
            ids = saved.get("ids")
            if not isinstance(ids, (list, tuple)):
                return None
            directory.setdefault((side, str(tag)), set()).update(str(item) for item in ids)
    return directory


def _identity(value):
    """A group identity from a bare name or an explicit (side, name) pair.

    Returns (identity, None), or (None, "ambiguous") when the value cannot be
    read as one.  A dict with visibility/name is accepted too, so a caller can
    be explicit without knowing the tuple spelling.
    """
    if isinstance(value, dict):
        name = value.get("name")
        side = _side(value.get("visibility", ""))
        if name is None or side is None:
            return None, "ambiguous"
        return (side, str(name)), None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        side = _side(value[0])
        if side is None:
            return None, "ambiguous"
        return (side, str(value[1])), None
    if isinstance(value, str) and value:
        return ("?", value), None
    return None, "ambiguous"


def _resolve(value, directories, artist_side=None):
    """Resolve one target to a concrete (side, name), or report ambiguity.

    A bare name that appears on both sides is not guessed: the website can
    hold two groups with the same name and different members, and merging
    them would turn "the user regrouped on the private side" into "the
    public side changed too".  When the name appears nowhere, the artist it
    belongs to is the only side that can make sense of it.
    """
    identity, problem = _identity(value)
    if identity is None:
        return None, problem
    side, name = identity
    if side != "?":
        return (side, name), None
    sides = set()
    for directory in directories:
        if not directory:
            continue
        for candidate_side, candidate_name in directory:
            if candidate_name == name:
                sides.add(candidate_side)
    if len(sides) > 1:
        return None, "ambiguous"
    if not sides:
        return ((artist_side, name) if artist_side else (None, name)), None
    return (next(iter(sides)), name), None


def _plan_rows(plan):
    """artist_id -> (target entries, groups recorded at planning time, row)."""
    rows = {}
    for row in plan.get("artists") or ():
        if not isinstance(row, dict):
            continue
        artist_id = row.get("artist_id")
        if artist_id is None:
            continue
        rows[str(artist_id)] = (
            list(row.get("target") or ()),
            sorted({str(name) for name in (row.get("groups") or ())}),
            row,
        )
    return rows


def renames(before, after):
    """(side, old) -> (side, new), but only where the member sets are identical."""
    old = _directory(before) or {}
    new = _directory(after) or {}
    dropped = sorted(set(old) - set(new))
    appeared = sorted(set(new) - set(old))
    mapping = {}
    for old_key in dropped:
        for new_key in appeared:
            if new_key in mapping.values() or old_key[0] != new_key[0]:
                continue
            if old[old_key] == new[new_key]:
                mapping[old_key] = new_key
                break
    return mapping


def _decision(artist_id, kind, reason, before_groups, after_groups, observed_at):
    return {
        "artist_id": artist_id,
        "kind": kind,
        "action": ACTION_FOR[kind],
        "reason": reason,
        "needs_human": ACTION_FOR[kind] == "needs_human",
        "evidence": {
            "before": list(before_groups or ()),
            "after": list(after_groups or ()),
            "observed_at": str(observed_at or ""),
        },
    }


def decide(before, after, plan, *, observed_at=""):
    """One decision per artist the plan or the newer snapshot talks about."""
    rows = _plan_rows(plan)
    old_directory = _directory(before)
    new_directory = _directory(after)
    # A partition we could not read is not a partition that shrank.  Without
    # both directories there is no "deleted" and no "renamed" to speak of.
    readable = old_directory is not None and new_directory is not None
    renamed = renames(before, after) if readable else {}
    if readable:
        deleted_keys = set(old_directory) - set(new_directory) - set(renamed)
    else:
        deleted_keys = set()
    joined = (
        bool(before.get("memberships_complete"))
        and bool(after.get("memberships_complete"))
        and readable
    )

    decisions = []
    for artist_id in sorted(rows):
        targets, planned_from, _row = rows[artist_id]
        current = _groups_of(after, artist_id)
        present_before = _groups_of(before, artist_id) is not None

        if artist_id not in (after.get("artists") or {}):
            if not joined:
                decisions.append(_decision(artist_id, "unknown", "snapshot_incomplete",
                                           planned_from, None, observed_at))
            else:
                decisions.append(_decision(artist_id, "artist_gone",
                                           "artist_not_followed_any_more",
                                           planned_from, None, observed_at))
            continue
        if not joined or current is None or not present_before:
            decisions.append(_decision(artist_id, "unknown", "snapshot_incomplete",
                                       planned_from, current, observed_at))
            continue

        artist_side = _side(((_row or {}).get("visibility", "")))
        resolved, problem = [], None
        for entry in targets:
            identity, issue = _resolve(entry, (new_directory, old_directory), artist_side)
            if issue:
                problem = issue
                break
            resolved.append(identity)
        if problem:
            decisions.append(_decision(artist_id, "unknown", "group_ambiguous",
                                       planned_from, current, observed_at))
            continue
        if any(identity in deleted_keys for identity in resolved):
            decisions.append(_decision(artist_id, "group_deleted", "target_group_deleted",
                                       planned_from, current, observed_at))
            continue
        if any(identity in renamed for identity in resolved):
            decisions.append(_decision(artist_id, "group_renamed", "target_group_renamed",
                                       planned_from, current, observed_at))
            continue
        if current != planned_from:
            decisions.append(_decision(artist_id, "moved", "website_groups_changed",
                                       planned_from, current, observed_at))
            continue
        decisions.append(_decision(artist_id, "unchanged", "same_as_planned",
                                   planned_from, current, observed_at))

    # "New" means appeared since the previous snapshot.  An artist who was
    # already there but is not in this plan is simply not in this batch.
    previous_ids = {str(key) for key in (before.get("artists") or {})}
    for artist_id in sorted(after.get("artists") or {}):
        artist_id = str(artist_id)
        if artist_id in rows or artist_id in previous_ids:
            continue
        current = _groups_of(after, artist_id)
        if not joined or current is None:
            decisions.append(_decision(artist_id, "unknown", "snapshot_incomplete",
                                       (), current, observed_at))
            continue
        decisions.append(_decision(artist_id, "new_member", "artist_appeared_since_snapshot",
                                   (), current, observed_at))
    return decisions


def writes_for(decisions):
    """Always empty.  This module never turns a conflict into a write."""
    return []


def summary(decisions):
    """kind -> count, with every kind present (0 when it did not happen)."""
    counts = {kind: 0 for kind in KINDS}
    for decision in decisions:
        counts[decision["kind"]] = counts.get(decision["kind"], 0) + 1
    return counts
