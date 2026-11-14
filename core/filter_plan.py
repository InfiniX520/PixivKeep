"""A saved filter plan as one immutable run snapshot (U2-B / B4).

A run reads this snapshot once and keeps using it even if the user edits the
plan while the run is in progress: *changing a plan affects the next
create/resume, never the task that is already executing*.  That promise only
holds if the snapshot cannot be mutated behind the run's back, so the object is
frozen, its parts are the frozen dataclasses from core.task_contracts, and the
free-form resource_start mapping is copied and wrapped in a read-only view.

The fingerprint is sha256 over canonical JSON: two plans that describe the same
thing get the same fingerprint whatever order their dictionaries were built in,
and any real change moves the fingerprint.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from core.task_contracts import (
    ContractError,
    FilterSpec,
    OutputSpec,
    PathRef,
    QualitySpec,
    canonical_json,
)

_RESOURCE_KEY_MAX = 64
_RESOURCE_SCALARS = (str, int, bool)


def _resource_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("resource_start keys must be non-empty strings")
    if len(value) > _RESOURCE_KEY_MAX:
        raise ContractError("resource_start key is too long")
    if any(ord(char) < 32 for char in value):
        raise ContractError("Control character in resource_start key")
    return value


def _freeze_resource_start(value: object) -> Mapping[str, Any]:
    """Copy one flat mapping of JSON scalars into a read-only view.

    Only scalars are accepted on purpose: a nested structure would be a second
    configuration language with its own validation, and a float would not
    survive canonical_json (which refuses it).  Rejecting both here keeps the
    snapshot's fingerprint honest.
    """
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ContractError("resource_start must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        name = _resource_key(key)
        if item is not None and type(item) not in _RESOURCE_SCALARS:
            raise ContractError(
                "resource_start accepts only str/int/bool/None values: %r" % (name,)
            )
        frozen[name] = item
    return MappingProxyType(frozen)


def _conditions(value: object) -> FilterSpec:
    if isinstance(value, FilterSpec):
        return value
    if isinstance(value, Mapping):
        unknown = set(value) - {"min_bookmarks", "exclude_ai"}
        if unknown:
            raise ContractError("Unknown filter condition: %r" % sorted(unknown))
        return FilterSpec(
            min_bookmarks=value.get("min_bookmarks"),
            exclude_ai=value.get("exclude_ai", False),
        )
    raise ContractError("conditions must be a FilterSpec or a mapping")


def _path_ref(value: object) -> PathRef:
    if isinstance(value, PathRef):
        return value
    if isinstance(value, Mapping):
        unknown = set(value) - {"kind", "root_id", "relative_path"}
        if unknown:
            raise ContractError("Unknown path reference field: %r" % sorted(unknown))
        return PathRef(
            kind=value.get("kind", "project"),
            root_id=value.get("root_id", "project"),
            relative_path=value.get("relative_path", "downl_Pixiv"),
        )
    raise ContractError("root_ref must be a PathRef or a mapping")


def _output_spec(value: object) -> OutputSpec:
    if isinstance(value, OutputSpec):
        return value
    if isinstance(value, Mapping):
        unknown = set(value) - {"root_ref", "folder_by_artist", "name_template"}
        if unknown:
            raise ContractError("Unknown output policy field: %r" % sorted(unknown))
        return OutputSpec(
            root_ref=_path_ref(value.get("root_ref", {})),
            folder_by_artist=value.get("folder_by_artist", True),
            name_template=value.get("name_template", "{illust_id}_p{page_index}"),
        )
    raise ContractError("output_policy must be an OutputSpec or a mapping")


def _quality_spec(value: object) -> QualitySpec:
    if isinstance(value, QualitySpec):
        return value
    if isinstance(value, Mapping):
        unknown = set(value) - {"original", "all_pages", "validation_policy_version"}
        if unknown:
            raise ContractError("Unknown integrity policy field: %r" % sorted(unknown))
        return QualitySpec(
            original=value.get("original", True),
            all_pages=value.get("all_pages", True),
            validation_policy_version=value.get("validation_policy_version", 1),
        )
    raise ContractError("integrity_policy must be a QualitySpec or a mapping")


@dataclass(frozen=True, slots=True)
class FilterPlan:
    """The four parts of a saved plan, frozen for the lifetime of one run.

    A run reads this object once; changing the plan afterwards affects the
    next create or resume, never the task that is already executing.  That is
    why every part is a frozen dataclass and why ``resource_start`` is a
    read-only view: a snapshot that can be edited is not a snapshot.
    """

    conditions: FilterSpec = field(default_factory=FilterSpec)
    output_policy: OutputSpec = field(default_factory=OutputSpec)
    integrity_policy: QualitySpec = field(default_factory=QualitySpec)
    resource_start: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, kind in (
            ("conditions", FilterSpec),
            ("output_policy", OutputSpec),
            ("integrity_policy", QualitySpec),
        ):
            value = getattr(self, name)
            if not isinstance(value, kind):
                raise ContractError("%s must be a %s" % (name, kind.__name__))
        object.__setattr__(
            self, "resource_start", _freeze_resource_start(self.resource_start)
        )

    # -- serialisation ---------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """A plain, mutable copy: callers may keep it, the plan does not change."""
        root = self.output_policy.root_ref
        return {
            "conditions": {
                "min_bookmarks": self.conditions.min_bookmarks,
                "exclude_ai": self.conditions.exclude_ai,
            },
            "output_policy": {
                "root_ref": {
                    "kind": root.kind,
                    "root_id": root.root_id,
                    "relative_path": root.relative_path,
                },
                "folder_by_artist": self.output_policy.folder_by_artist,
                "name_template": self.output_policy.name_template,
            },
            "integrity_policy": {
                "original": self.integrity_policy.original,
                "all_pages": self.integrity_policy.all_pages,
                "validation_policy_version": self.integrity_policy.validation_policy_version,
            },
            "resource_start": dict(self.resource_start),
        }

    @classmethod
    def from_dict(cls, value: object) -> "FilterPlan":
        if not isinstance(value, Mapping):
            raise ContractError("A plan must be a mapping")
        unknown = set(value) - {
            "conditions",
            "output_policy",
            "integrity_policy",
            "resource_start",
        }
        if unknown:
            raise ContractError("Unknown plan field: %r" % sorted(unknown))
        return cls(
            conditions=_conditions(value.get("conditions", {})),
            output_policy=_output_spec(value.get("output_policy", {})),
            integrity_policy=_quality_spec(value.get("integrity_policy", {})),
            resource_start=value.get("resource_start", {}),
        )

    # -- identity --------------------------------------------------------
    def fingerprint(self) -> str:
        """sha256 of canonical JSON; independent of dictionary insertion order."""
        return hashlib.sha256(
            canonical_json(self.as_dict()).encode("utf-8")
        ).hexdigest()

    # -- derivation ------------------------------------------------------
    def with_conditions(self, new_conditions: object) -> "FilterPlan":
        """A new plan with different conditions; this object stays as it was."""
        return FilterPlan(
            conditions=_conditions(new_conditions),
            output_policy=self.output_policy,
            integrity_policy=self.integrity_policy,
            resource_start=dict(self.resource_start),
        )
