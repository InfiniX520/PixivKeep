"""Rollback material for the shared interpreter: generated, never executed.

A dependency upgrade in a shared environment is only safe when the previous
state can be described exactly and rebuilt.  This module turns the facts that
dependency_env.environment_facts() already collected into such a description:
the frozen package list, the interpreter that owns it, the steps a human would
follow to restore it, and the conditions that must hold before anyone touches
the environment at all.

Nothing here runs a command, touches a file or changes a variable.  The steps
are text for a person; the module has no code path that could install,
uninstall or upgrade anything, and a test asserts that by walking its own AST.

Contract (U1-B five-way split, lane R):
    rollback_material(env, *, at) -> {
      "packages": [{"name", "version"}],     # sorted, de-duplicated by name
      "interpreter": {"executable", "version"},
      "restore_steps": [str],
      "window_conditions": [str],
      "three_states": {"advisory_matched", "upgrade_done", "compat_checked"}
    }

The three states are deliberately separate, because 'the advisory was read',
'the upgrade happened' and 'compatibility was checked' are three different
facts and a single boolean would let one stand in for another.  Their defaults
describe a batch that prepared the material and changed nothing.
"""
from __future__ import annotations

import datetime
from collections.abc import Mapping

#: What a preparation-only batch reports.  'unknown' is not 'clean', and
#: 'not_run' is not 'passed'.
DEFAULT_STATES = {
    "advisory_matched": "unknown",
    "upgrade_done": False,
    "compat_checked": "not_run",
}

_STATE_KEYS = tuple(DEFAULT_STATES)

#: Projects that share the interpreter; a replacement must not run while any of
#: them is working.
_SHARED_PROJECTS = (
    "JpgPng",
    "arknights_story",
    "zspms_story",
    "File_Proc",
    "wechat-mp-tools",
    "xiaohongshu_corpus",
)


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _packages(env: object) -> list[dict[str, object]]:
    """The frozen package list, sorted and de-duplicated by name.

    A name that appears twice keeps the last version seen: a listing that
    contradicts itself must not be presented as two different packages.
    """
    if not isinstance(env, Mapping):
        return []
    raw = env.get("installed")
    if not isinstance(raw, list):
        return []
    seen: dict[str, object] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        version = item.get("version")
        seen[name.strip()] = None if version is None else str(version)
    return [{"name": name, "version": seen[name]} for name in sorted(seen)]


def _interpreter(env: object) -> dict[str, object]:
    """The interpreter a restore must use, or explicit nulls when unknown."""
    python = env.get("python") if isinstance(env, Mapping) else None
    if not isinstance(python, Mapping):
        return {"executable": None, "version": None}
    executable = python.get("executable")
    version = python.get("version")
    return {
        "executable": executable.strip()
        if isinstance(executable, str) and executable.strip()
        else None,
        "version": version.strip()
        if isinstance(version, str) and version.strip()
        else None,
    }


def _states(at: object) -> dict[str, object]:
    """Default three states, optionally overridden by the caller.

    Overrides exist for the batch that really does upgrade something later; a
    caller can then say upgrade_done=True and compat_checked="passed" without
    this module ever gaining a code path that performs the change.
    """
    states = dict(DEFAULT_STATES)
    if at is None:
        return states
    if not isinstance(at, Mapping):
        raise TypeError("at must be a mapping of three-state overrides or None")
    unknown = sorted(key for key in at if key not in _STATE_KEYS)
    if unknown:
        raise ValueError(
            "unknown three-state key(s): %s (allowed: %s)"
            % (", ".join(str(item) for item in unknown), ", ".join(_STATE_KEYS))
        )
    for key in _STATE_KEYS:
        if key not in at:
            continue
        value = at[key]
        if key == "upgrade_done":
            if not isinstance(value, bool):
                raise ValueError("upgrade_done must be a bool, not %r" % (value,))
            states[key] = value
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s must be a non-empty string, not %r" % (key, value))
        states[key] = value.strip()
    return states


def _restore_steps(packages: list[dict[str, object]], interpreter: Mapping) -> list[str]:
    """Human steps that rebuild exactly this package list on this interpreter."""
    executable = interpreter.get("executable")
    version = interpreter.get("version")
    who = executable if executable else "（环境事实里没有 executable，先确认是哪一个解释器）"
    frozen_name = "requirements-frozen-%s.txt" % _now_utc()[:10]
    steps = [
        "先确认共享解释器的其它在用项目（%s）都没有在运行；有任何一个在跑就不要动手。"
        % "、".join(_SHARED_PROJECTS),
        "把下面的冻结清单另存为 %s（%d 个包），这份文件就是回退依据，不要只留在报告里。"
        % (frozen_name, len(packages)),
        "用同一个解释器重装：%s -m pip install --no-deps -r %s"
        % (who, frozen_name),
        "不要在重装时换解释器，也不要用 pip install --user；版本以冻结清单为准%s。"
        % ("（记录的解释器版本：%s）" % version if version else ""),
        "重装后运行 %s -m pip check 与项目测试；任一步失败，按同一份冻结清单再装一遍回到原状态。"
        % (who,),
        "回退靠重装，不靠删文件：不要删除 wheel 缓存、site-packages 目录或任何已安装目录。",
        "如果冻结清单本身缺失或不可读，先重建清单（重新采集环境事实）再谈升级，不要凭记忆恢复。",
    ]
    return steps


def _window_conditions() -> list[str]:
    """What must hold before anyone is allowed to change this environment."""
    return [
        "本批次不做任何安装、升级或卸载：用户要求只在本目录工作，共享解释器在目录之外。",
        "真正升级需要四件事同时成立：用户明确同意、独立维护窗口、其它在用项目全部停止、目标版本与公告已核对。",
        "升级不得与功能改动混在同一次不可回退的提交里；回退材料必须先于升级保存。",
        "升级前保存本材料（冻结清单 + 解释器），并确认能按它重装；确认不了就不要升级。",
        "pip check 非零时不要升级：先解释现有冲突，再谈变更。",
        "升级后必须分别记录公告核对、实际升级、兼容检查三项事实，不能只改一行版本下限就算完成。",
    ]


def rollback_material(env: object, *, at: object = None) -> dict[str, object]:
    """Describe how to rebuild the environment that env came from.

    Returns the frozen package list, the interpreter, restore steps for a
    person, the conditions that gate an upgrade, and the three states.  It
    never executes, writes or mutates: env is only read, and the caller's
    mapping is not modified.
    """
    packages = _packages(env)
    interpreter = _interpreter(env)
    return {
        "packages": packages,
        "interpreter": interpreter,
        "restore_steps": _restore_steps(packages, interpreter),
        "window_conditions": _window_conditions(),
        "three_states": _states(at),
    }
