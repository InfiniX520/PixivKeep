# U2-B 筛选解释、离线重评估与方案快照：口径

对象：**解释**一个候选为什么被排除或为什么判不了，**离线重算**改条件会影响哪些候选，以及把一套筛选条件**冻结成快照**。
三件事都不碰文件：不删、不移、不改名、不下载。

契约与五路拆分见 [U2B_FIVE_WAY.md](U2B_FIVE_WAY.md)（含合流时对齐的口径修订）。

## 1. 模块分工

| 模块 | 职责 |
| --- | --- |
| `core/filter_reasons.py` | 原因码闭集、字段闭集、冻结的原因记录、旧码映射表、`verdict_of` |
| `core/filter_explain.py` | `explain(candidate, conditions)`：复用既有 `evaluate_filters` 的结论并翻译 |
| `core/filter_reevaluate.py` | `reevaluate(candidates, old, new, *, explain=None)`：五个桶 + `uncovered_reasons` |
| `core/filter_plan.py` | 不可变 `FilterPlan`（条件 + 输出 + 完整性 + 资源起点）+ 指纹 |

## 2. 原因码：封闭集合

| 码 | 字段 | 含义 | 是否 unknown |
| --- | --- | --- | --- |
| `favorites_below` | bookmarks | 收藏数低于阈值 | 否 |
| `ai_excluded` | ai_flag | 命中 AI 排除 | 否 |
| `type_unsupported` | type | 作品类型不支持（ugoira） | 否 |
| `field_unknown` | bookmarks / ai_flag / type / original_urls | 该字段缺失或不可解析 | **是** |
| `access_denied` | availability | 明确不可见 | 否 |

字段闭集是 `bookmarks / ai_flag / type / availability / original_urls`。闭集外一律 `ValueError`，**不存在"自由字符串原因"**。

旧码到新码是一张**数据表**（`LEGACY_CODE_MAP`，八项，两处副本由矩阵断言逐字相同）：

| 旧码（既有实现可能发出的） | 新码 | 字段 | unknown |
| --- | --- | --- | --- |
| bookmarks_below_minimum | favorites_below | bookmarks | 否 |
| bookmarks_unknown | field_unknown | bookmarks | 是 |
| ai_excluded | ai_excluded | ai_flag | 否 |
| ai_flag_unknown | field_unknown | ai_flag | 是 |
| ugoira_unsupported | type_unsupported | type | 否 |
| artwork_unavailable | access_denied | availability | 否 |
| original_metadata_unknown | field_unknown | original_urls | 是 |
| invalid_original_metadata | field_unknown | original_urls | 是 |

**边界口径**（有意为之，各有测试）：

- `kind == "ugoira"` 是 `type_unsupported`；`kind == "unknown"` 是 `field_unknown(type)`。两者语义不同：一个"类型不支持"，一个"类型没读懂"。
- `available is False` → `access_denied`；`available is None` **不产生原因**（与既有 `make_candidate` 一致：未知可达性不等于被拒绝，也不阻塞判定）。
- 收藏数缺失**不当 0**：有阈值时是 `field_unknown`，没有阈值时根本不检查。
- 已知拒绝优先于未知：同时"低于阈值"和"AI 标志读不出"的候选判 `exclude`，不是 `unknown`。

## 3. 三态与五桶

`explain` 的三态映射写死为一张表：`allow→match`、`filtered→exclude`、`metadata_unknown→unknown`。

`reevaluate` 的规则是**新判定决定桶，旧判定只用来细分**：

| 新判定 | 旧判定 | 桶 |
| --- | --- | --- |
| match | match | `kept` |
| match | exclude 或 unknown | `newly_included`（放宽后被重新纳入） |
| exclude | match | `newly_excluded` |
| exclude | exclude 或 unknown | `still_excluded` |
| unknown | 任意 | `uncovered` |

因此 `uncovered` 恰好是「在新条件下仍判不了」的那批，`uncovered_reasons` 的键集与之相同。
五桶之和恒等于候选数（有参数化测试），且**一件文件都不会被动**（模块不 import os/shutil/pathlib，不出现删除/移动调用，AST 断言）。

## 4. 方案快照

`FilterPlan` 是 `frozen=True, slots=True` 的四部件快照：`conditions`(FilterSpec)、`output_policy`(OutputSpec)、
`integrity_policy`(QualitySpec)、`resource_start`（只收 str/int/bool/None 的扁平映射，构造时深拷贝并冻结）。

- `fingerprint()` = `sha256(canonical_json(as_dict()))`，与字典插入顺序无关；任何真实改动都会换指纹。
- `with_conditions(new)` 返回**新对象**，旧对象的字段、`as_dict()`、指纹三者都不变。
- 语义：运行中的任务读取的是它创建时的快照；改方案只影响**下一次**创建或续接，不会暗改已经在跑的任务。

## 5. 九份合成夹具的实际结果

夹具在 `tests/filter_fixtures.py`（九个场景：bookmarks_below / ai_excluded / ugoira / kind_unknown /
bookmarks_missing / ai_flag_missing / unavailable / clean_match / no_conditions）。

条件由「无筛选」收紧到 `min_bookmarks=100 & exclude_ai=True`：

| 桶 | 候选 |
| --- | --- |
| kept | clean_match |
| newly_excluded | no_conditions、bookmarks_below、ai_excluded |
| still_excluded | ugoira、unavailable |
| uncovered | kind_unknown、bookmarks_missing、ai_flag_missing |
| newly_included | （无） |

再把条件放宽回去：`newly_included` = bookmarks_below、ai_excluded、no_conditions、bookmarks_missing、ai_flag_missing；
`still_excluded` = ugoira、unavailable；`uncovered` = kind_unknown；`kept` = clean_match。
这两组断言就是 `tests/test_filter_matrix.py` 里钉住的东西。

## 6. 已知边界

1. **没有 GUI/CLI 接线**。本轮交付的是引擎与测试：把覆盖摘要与筛选解释接进界面需要新的视图设计与 Tk 验证，而 Tk 在本机有已知的间歇性 Tcl 初始化故障（见验收记录）。接线单独排期，不在本轮冒充已完成。
2. `explain` 只做单候选；批量解释由调用方循环（`reevaluate` 就是 2N 次调用的例子）。
3. 不支持用户表达式、不支持自由代码；标签只做字面匹配。
4. 旧码表是静态的：若既有实现将来新增第九个码，`explain` 会**响亮报错**`unmapped_reason_code`，而不是把它当自由字符串透传给用户。
5. `resource_start` 只收扁平标量；嵌套结构等于第二套配置语言，明确不支持。
