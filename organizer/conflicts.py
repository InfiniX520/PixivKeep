"""What the website says now, versus what the last round planned to do.

The website is the authority.  A user may regroup an artist, rename a group or
delete one at any time, and this module's whole job is to notice that *before*
anything is written and to say "keep the website, ask a human" instead of
pushing an older plan over it.

Two properties are the point of the design:

* **it never emits a write.**  Every decision carries an action from a closed
  table, and none of those actions is "write".  `writes_for` returns [] for any
  input; the tests pin that, because "we only keep things" is easy to say and
  easy to lose in a later edit.
* **unknown is not "unchanged".**  If either snapshot is incomplete, or an
  artist the plan talks about is missing from the current snapshot, the answer
  is `unknown` and a human decides.  Treating a missing fact as "nothing
  changed" is how an old plan ends up overwriting a manual edit.

The snapshots and the plan are the shapes organizer/snapshots.py and
organizer/plans.py already persist (see E1_PLAN.md 3.4).  Nothing here opens a
file, a database or a socket; the caller passes plain dictionaries.
"""
from __future__ import annotations

KINDS = ("unchanged", "moved", "group_renamed", "group_deleted", "new_member", "unknown")
ACTIONS = ("keep", "replan", "needs_human")

#: kind -> action.  A closed table, so a new kind cannot quietly default to
#: something optimistic: adding one here without an action fails the import test.
ACTION_FOR = {
    "unchanged": "keep",
    "moved": "needs_human",
    "group_renamed": "needs_human",
    "group_deleted": "needs_human",
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
    "snapshot_incomplete",
    "membership_missing",
)


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
    """group name -> set of member artist ids, from the membership partitions."""
    directory = {}
    partitions = snapshot.get("memberships") or {}
    if not isinstance(partitions, dict):
        return None
    for side in ("show", "hide"):
        side_partition = partitions.get(side) or {}
        if not isinstance(side_partition, dict):
            return None
        for tag, saved in side_partition.items():
            if not isinstance(saved, dict):
                return None
            ids = saved.get("ids")
            if not isinstance(ids, (list, tuple)):
                return None
            directory.setdefault(str(tag), set()).update(str(item) for item in ids)
    return directory


def _plan_targets(plan):
    """artist_id -> (sorted target groups, sorted groups at planning time)."""
    targets = {}
    for row in plan.get("artists") or ():
        if not isinstance(row, dict):
            continue
        artist_id = row.get("artist_id")
        if artist_id is None:
            continue
        target = row.get("target")
        before = row.get("groups")
        targets[str(artist_id)] = (
            sorted({str(name) for name in (target or ())}),
            sorted({str(name) for name in (before or ())}),
        )
    return targets


def renames(before, after):
    """old name -> new name, but only where the member sets are identical.

    A group is identified by its name and nothing else (the website exposes no
    stable group id), so "old name gone, new name in its place, same members" is
    the only rename this module is willing to believe.  Anything else stays a
    deletion plus an addition, and a deletion is never recreated.
    """
    old = _directory(before) or {}
    new = _directory(after) or {}
    dropped = sorted(set(old) - set(new))
    appeared = sorted(set(new) - set(old))
    mapping = {}
    for old_name in dropped:
        for new_name in appeared:
            if new_name in mapping.values():
                continue
            if old[old_name] == new[new_name]:
                mapping[old_name] = new_name
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
    """One decision per artist the plan or the newer snapshot talks about.

    Deterministic and order-independent: the result is sorted by artist id, and
    it depends only on the three arguments (they are read, never modified).
    """
    targets = _plan_targets(plan)
    old_directory = _directory(before)
    new_directory = _directory(after)
    # A partition we could not read is not a partition that shrank.  Without
    # both directories there is no "deleted" and no "renamed" to speak of,
    # only an incomplete picture.
    readable = old_directory is not None and new_directory is not None
    renamed = renames(before, after) if readable else {}
    if readable:
        deleted_after_rename = set(old_directory) - set(new_directory) - set(renamed)
    else:
        deleted_after_rename = set()
    complete = (
        bool(before.get("memberships_complete"))
        and bool(after.get("memberships_complete"))
        and readable
    )

    decisions = []
    seen = set()
    for artist_id in sorted(targets):
        desired, planned_from = targets[artist_id]
        current = _groups_of(after, artist_id)
        previous = _groups_of(before, artist_id)
        seen.add(artist_id)

        if not complete or current is None or previous is None:
            reason = "snapshot_incomplete" if not complete else "membership_missing"
            decisions.append(
                _decision(artist_id, "unknown", reason, planned_from, current, observed_at)
            )
            continue
        gone_names = [name for name in desired if name in deleted_after_rename]
        if gone_names:
            decisions.append(_decision(artist_id, "group_deleted", "target_group_deleted",
                                       planned_from, current, observed_at))
            continue
        if any(name in renamed for name in desired):
            decisions.append(_decision(artist_id, "group_renamed", "target_group_renamed",
                                       planned_from, current, observed_at))
            continue
        if current != planned_from:
            # The user regrouped this artist after the plan was made.  The
            # website wins; the plan is not re-applied and a human decides.
            decisions.append(_decision(artist_id, "moved", "website_groups_changed",
                                       planned_from, current, observed_at))
            continue
        decisions.append(_decision(artist_id, "unchanged", "same_as_planned",
                                   planned_from, current, observed_at))

    # "New" means appeared since the previous snapshot.  An artist who was
    # already there but is not in this plan is simply not in this batch, which
    # is not a decision at all.
    previous_ids = {str(key) for key in (before.get("artists") or {})}
    for artist_id in sorted((after.get("artists") or {})):
        artist_id = str(artist_id)
        if artist_id in seen or artist_id in previous_ids:
            continue
        current = _groups_of(after, artist_id)
        if not complete or current is None:
            decisions.append(_decision(artist_id, "unknown", "snapshot_incomplete",
                                       (), current, observed_at))
            continue
        decisions.append(_decision(artist_id, "new_member", "artist_appeared_since_snapshot",
                                   (), current, observed_at))
    return decisions


def writes_for(decisions):
    """Always empty.  This module never turns a conflict into a write.

    Kept as a real function (rather than a comment) so a caller can assert the
    contract on its own decisions, and so a future "replan" action cannot start
    writing here without this function changing too.
    """
    return []


def summary(decisions):
    """kind -> count, with every kind present (0 when it did not happen)."""
    counts = {kind: 0 for kind in KINDS}
    for decision in decisions:
        counts[decision["kind"]] = counts.get(decision["kind"], 0) + 1
    return counts
