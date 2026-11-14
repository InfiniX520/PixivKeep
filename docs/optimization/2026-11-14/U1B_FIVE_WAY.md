# U1-B 五路拆分（一个超级节点 → 5 条并发子节点）

基线：`a9fc470`。本轮只做 **U1-B**（依赖与发布可回退性），拆成 5 条文件不重叠的子节点并发推进；主线程负责组合器、CLI 与文档。

## 0. 冻结的模块契约（5 条 lane 必须各自实现其中一个，接口不许改）

```text
tools/maintenance/dependency_env.py
    environment_facts() -> {
      "python": {version, implementation, executable, env_prefix, is_conda},
      "platform": {system, release, machine},
      "pip_check": {exit, output},
      "installed": [{name, version}],          # importlib.metadata，按名称排序
      "generated_at": iso8601
    }

tools/maintenance/dependency_declared.py
    declared_requirements(root: Path) -> {
      "files": [{path, kind}],                 # requirements*.txt / pyproject.toml
      "runtime": [{name, specifier}], "dev": [...], "build": [...],
      "errors": [{file, reason}],              # 解析不了就进这里，不抛
      "generated_at": iso8601
    }

tools/maintenance/dependency_imports.py
    imported_modules(root: Path) -> {
      "modules": [{top_level, files:[...], count,
                   stdlib: bool, resolved: {origin} | null, unresolved_reason}],
      "generated_at": iso8601
    }

tools/maintenance/dependency_matrix.py
    compare(declared, installed, imported) -> {
      "missing": [{name, specifier}],          # 声明了但没装
      "unexpected": [{name, version}],         # 装了但既没声明也没 import
      "unused_declared": [{name}],             # 声明了但代码不 import
      "version_mismatch": [{name, declared, installed}],
      "verdict": "ok" | "attention" | "unknown"
    }

tools/maintenance/dependency_rollback.py
    rollback_material(env, *, at) -> {
      "packages": [{name, version}],
      "interpreter": {executable, version},
      "restore_steps": [str],                  # 只描述，不执行
      "window_conditions": [str],              # 何时才允许升级
      "three_states": {advisory_matched, upgrade_done, compat_checked}
    }
```

**共同硬约束**：五个模块都必须**只读**——不得出现 `pip install/uninstall`、`subprocess` 调用 pip 的变更子命令、写环境变量、改文件；只用 `importlib.metadata`、`sys`、`platform`、`ast`、`pathlib`、`subprocess` 读 `pip check`（只读子命令）。每条 lane 自带一条 AST 断言测试证明这一点。

## 1. 五条子节点

| lane | 独占文件 | 完成定义 |
| --- | --- | --- |
| E（环境事实） | `tools/maintenance/dependency_env.py`、`tests/test_dependency_env.py` | 真实环境跑一次返回全部键；`pip check` 非零时如实记录；monkeypatch 伪造 metadata 时不崩 |
| D（声明解析） | `tools/maintenance/dependency_declared.py`、`tests/test_dependency_declared.py` | 正确解析现有四个文件；坏行/坏 TOML 进 errors 不抛；`-r requirements.txt` 的 include 被展开 |
| I（import 扫描） | `tools/maintenance/dependency_imports.py`、`tests/test_dependency_imports.py` | 扫 `core/gui/utils`；区分 stdlib/第三方/本地包；能给出实际解析路径；语法错误文件进 unresolved 不崩 |
| M（矩阵比对） | `tools/maintenance/dependency_matrix.py`、`tests/test_dependency_matrix.py` | 四类差异各自可造；verdict 三态；纯函数（输入输出都是 dict，不读磁盘） |
| R（回退材料） | `tools/maintenance/dependency_rollback.py`、`tests/test_dependency_rollback.py` | 生成包清单 + 恢复步骤 + 窗口条件 + 三态；`upgrade_done=false` 且理由可配；不执行任何恢复动作 |

## 2. 主线程（我）在合流后做

`tools/maintenance/dependency_report.py`：把五个模块串起来 → 一份 JSON + 一段文本；CLI：`python tools/maintenance/dependency_report.py [--json]`；文档 `docs/maintenance/2026-11-14_u1b_dependency_maintenance.md`（三态表 + 回退材料 + "未升级"的说明）。

## 3. 验收

- 五条 lane 各自测试全绿、AST 只读断言通过；
- 合流后组合器在真实环境跑一次（只读），输出写进文档；
- 全量矩阵与三个 Tk 模块照既定口径重跑，不回退任何旧断言；
- 只读增量复审无 P1/P2。

## 4. 之后

U1-B 收口后进入 **U1-C**，同样先拆五路（证据模型 / PNG 解码 / 上限与取消 / 解码器注册与能力门 / 集成适配），再并发推进。
