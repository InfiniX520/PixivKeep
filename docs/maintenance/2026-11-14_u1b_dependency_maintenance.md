# U1-B 维护记录：共享依赖与发布可回退性（2026-11-14）

基线 `ef12a56`（U1-B 五路拆分）→ 五条 lane 合流 `381c66f` → 组合器与本文档。
**本批次只读**：没有安装、升级、卸载任何包，没有修改环境变量，没有碰 `D:\Miniconda3` 之外的任何东西（见"为什么没有升级"）。

## 1. 三态（分开记录，不合并成"已修复"）

| 状态 | 值 | 说明 |
| --- | --- | --- |
| `advisory_matched` | **unknown** | 本批**没有**联网核对 CVE/公告。审计日期的公告匹配不能代替届时核对，所以这里是 unknown 而不是"已核对"。 |
| `upgrade_done` | **false** | 本批没有升级任何包。 |
| `compat_checked` | **not_run** | 没有升级，因此没有"升级后兼容检查"。`pip check` 的 exit=0 是**当前环境**的健康事实，不是升级兼容性结论。 |

## 2. 真实环境只读快照（`python tools/maintenance/dependency_report.py`）

| 项 | 值 |
| --- | --- |
| 解释器 | CPython 3.12.14，`D:\Miniconda3\envs\JuiLiu\python.exe`，conda 环境 |
| 平台 | Windows 11 / AMD64 |
| `pip check` | exit 0，"No broken requirements found." |
| 已装分发包 | 69（元数据读不出的：0） |
| 声明 | runtime 6 / dev 11 / build 7（来自 4 个文件） |
| import 扫描 | 62 个顶层模块（`core/gui/utils`），语法/编码不可解析文件 0 |
| 差异 | missing 4 / unexpected 61 / unused 4 / version_mismatch 0 → **verdict=ok**（只看 runtime） |

具体：

- **missing（4）**：`black`、`flake8`、`isort`、`pytest-cov` —— 都是 dev 组工具，当前环境没装。**不影响运行时**，但意味着 `pyproject.toml` 里的 isort/black 配置目前在本机跑不起来。
- **unexpected（61）**：装了但既没声明也没直接 import。**这是传递依赖的清单，不是缺陷清单**（本机 61 个全部是被 pixivpy3 / requests / selenium 等拖进来的）。
- **unused_declared（4）**：`cloudscraper`（runtime）、`requests-toolbelt`（runtime）、`pytest`（dev）、`pyinstaller`（build）。
  - runtime 两项是**真事实**：`requirements.txt` 声明了它们，但 `core/gui/utils` 里没有任何 import。要么是给将来的 Cloudflare 兜底留的，要么是死声明——**需要你决定**（我不擅自删，也不擅自加 import）。
  - dev/build 两项属扫描范围限制（见 §5）。
- **version_mismatch（0）**：所有已安装且声明的包都满足 specifier。

## 3. 回退材料（`dependency_rollback.py`）

- `packages`：69 条 `{name, version}`，按名排序、同名去重（版本原样透传，`null` 表示"装了但版本不明"）。
- `interpreter`：`{executable, version}`。
- `restore_steps`：7 条人读步骤，点名解释器、`requirements-frozen-<UTC 日期>.txt`、包数、`--no-deps`，并把 `pip install --user` 写成**禁止项**。
- `window_conditions`：6 条前置条件（其它在用项目未运行、用户明确同意、`pip check` 非零时不要升级……）。
- **未执行任何恢复动作**，本轮也不可能执行（只读约束）。

## 4. 为什么没有升级（这条要能回答"为什么还没修"）

1. 用户本轮明确要求**只在本目录工作**；共享解释器在 `D:\Miniconda3\envs\JuiLiu`，属于目录之外。
2. 工作区约定要求"共用解释器不重复建环境、不用 `pip install --user`"；升级共享环境会影响 `arknights_story`、`zspms_story`、`File_Proc`、`wechat-mp-tools` 等**正在用**的项目。
3. 设计文档要求 U1-B 单独成批、留回退材料、先确认在用状态——这些材料已备齐（§3），但"真的动手"必须另开一个维护窗口并得到你同意。

## 5. 诚实列出的限制

1. `[project.optional-dependencies]`（extras）**没有落点**：契约只有 runtime/dev/build 三个桶，D 选择忽略并在 docstring 写明。将来若用 extras，会静默丢失，需要在下一版契约加桶。
2. extras 被折进 specifier（`pkg[pdf]>=1.0` → name=`pkg`、specifier=`[pdf]>=1.0`）：下游只能按 name 比较，不能把 specifier 当纯版本约束。
3. 环境标记留在 specifier 内（`>=1.0,<2 ; python_version < '3.11'`），矩阵只按 name 比对。
4. `errors.reason` 是单个字符串 `"<code>: <detail>"`，组合器按 code 分组需要自己切。
5. `pip check` 的"超时"与"pip 不存在"都记 `exit=-1`，只能看 output 文本区分——契约只有 `{exit, output}`，我没有在 lane 里私加字段（`metadata_errors` 是唯一的附加键，为的是不把坏元数据藏起来）。
6. `unexpected` 天然包含传递依赖；`unused_declared` 与 import 扫描范围强耦合（只扫 `core/gui/utils`，所以 `tests/` 里用的 dev 依赖会被报"未用"）。
7. 别名表只覆盖常见几个（yaml→pyyaml、PIL→pillow 等），import 名与发行名无关的包会被误报 unused。
8. `is_conda` 用 `conda-meta` 目录判定，是启发式而非官方 API。
9. **没有做过"真的升级再回退"的演练**：那需要改环境，本轮硬约束禁止。所以 `restore_steps` 是合理步骤，不是被验证过的步骤。
10. `unused_declared` 的 runtime 两项（`cloudscraper`、`requests-toolbelt`）需要你裁决保留还是移除。

## 6. 复现

```powershell
cd E:\Zero9\JuiLiu\JpgPng
$env:PYTHONNOUSERSITE = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 tools/maintenance/dependency_report.py          # 文本
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 tools/maintenance/dependency_report.py --json   # JSON
```

退出码：0 = verdict ok（runtime 完全满足）；2 = attention（缺 runtime 依赖或版本不符）；3 = unknown（输入不足/无法判定）。**任何情况下都不会改动环境**。
