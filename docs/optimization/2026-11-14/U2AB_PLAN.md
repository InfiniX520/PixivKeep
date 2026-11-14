# U2-A / U2-B 批次计划（冻结，引擎层）

基线：`72f817e`（U1-B/U1-C 计划冻结之后）。分支 `feat/a1-u2ab-20261114`。
上游设计：[DESIGN §4 U2-A](../optimization/2026-11-11/DESIGN.md)、[§5 U2-B](../optimization/2026-11-11/DESIGN.md)、[总表](../optimization/IMPLEMENTATION.md)。

## 0. 与 U1-B/U1-C 并行的边界

- 两条 lane **只做引擎层**，**禁止修改** `gui/**`、`core/collection_desktop.py`、`core/library_maintenance.py`、`core/delivery_*.py`。GUI 接线由主线程在两 lane 合并后串行完成（避免两条 lane 抢同一视图文件，也避开本机 Tk 拆除的间歇故障）。
- 与 U1-C 不共享文件：U2 不碰 `core/image_validation.py`、`core/decode_verification.py`。

## 1. U2-A：关注历史的覆盖与更新（引擎 + 只读摘要）

### 1.1 交付物

- 新建 `core/follow_coverage.py`：把一次关注历史遍历的事实收成一个**带范围的摘要**对象：
  ```text
  FollowCoverage:
    scope: {account_ref, visibility, snapshot_at, artist_ids, source, work_types, filter_version, scanned_from, scanned_to}
    enumeration: "complete" | "partial" | "stopped" | "failed"   # 来源遍历
    decision:    "applied" | "unknown_fields" | "skipped"        # 筛选判定
    delivery:    "complete" | "partial" | "missing"              # 保存/核验
    counts: {artists_checked, artists_confirmed, artists_pending, artists_failed_pages, artists_coverage_unverified,
             artworks_seen, pages_saved, pages_already_verified, excluded_by_filter, unknown_fields}
    stop_reason: "budget" | "rate_limited" | "cancel" | "error" | null
    page_end_evidence: {last_page_full: bool, next_cursor_present: bool, duplicate_pages: int}
  ```
- 三条不变量（要测试）：
  1. `enumeration != "complete"` 时**不得**报告"已查完整个关注历史"；`decision`/`delivery` 分别独立，不能用一个布尔合并；
  2. 预算结束 / 空交集 / 首屏全被过滤 **都不等于**远端全集已穷尽（摘要里必须有 `stop_reason` 或明确的 `partial`）；
  3. 不显示无法证明的分母（除数为未知时输出 `null`，不要输出估算值冒充分母）。
- 摘要**只读**：从既有 `follow_crawl_state`/`follow_repository` 的事实构建，不改断点、不推进游标、不写库。
- `docs/optimization/2026-11-14/U2A_COVERAGE_SUMMARY.md`：口径说明 + 示例文本（示例数字必须标明是示例）。

### 1.2 验收矩阵

账号范围/私密范围缺失时不猜；部分失败页 → `enumeration=partial` + `artists_failed_pages>0`；预算停止 → `stop_reason=budget`；重复分页 → `duplicate_pages>0` 且不被当新作品；旧候选保留、不因摘要丢失；`counts` 与既有仓库事实逐项对得上（用合成库验证）。

## 2. U2-B：筛选解释、保存方案与离线重评估（引擎）

### 2.1 交付物

- 新建 `core/filter_explain.py`：
  - `explain(candidate, conditions) -> {verdict: "match"|"exclude"|"unknown", reasons: [{code, field, evidence, unknown: bool}]}`；
  - 原因码固定集合：`favorites_below`、`ai_excluded`、`type_unsupported`、`field_unknown`、`access_denied`（新增码必须成表，不许自由字符串）；
  - **字段证据**：每个原因带来源字段与观察值；`field_unknown` 只在字段确实缺失/不可解析时出现，不得把"没查"写成未知或反之。
- 离线重评估：`reevaluate(candidates, old_conditions, new_conditions) -> {kept, newly_excluded, newly_included, uncovered}`——放宽时能离线把旧候选重新纳入（`newly_included`），收紧时只标记、不删除任何文件。
- 保存方案（`core/filter_plan.py` 或同文件内的纯数据类）：`FilterPlan{conditions, output_policy, integrity_policy, resource_start}`，**不可变**；运行任务读取快照，改方案只影响下一次创建/续接。
- 不执行用户表达式；不支持自由代码；标签只做字面匹配（规范化值另存）。

### 2.2 验收矩阵

命中/排除/未知三态；未知字段不被当 0；收藏数有观察时间；`type_unsupported` 与 `field_unknown` 不混；放宽 → 旧候选被重新纳入且不重下已存在文件；收紧 → 不删文件；方案快照运行中不可变；原因码集合封闭（新增码有测试）。

## 3. 归属

| lane | 独占文件 |
| --- | --- |
| U2-A | `core/follow_coverage.py`（新建）、`tests/test_follow_coverage.py`（新建）、`docs/optimization/2026-11-14/U2A_COVERAGE_SUMMARY.md`（新建）；只读引用 `core/follow_*.py`，不改它们 |
| U2-B | `core/filter_explain.py`（新建）、`tests/test_filter_explain.py`（新建）、`docs/optimization/2026-11-14/U2B_FILTER_EXPLAIN.md`（新建）；只读引用 `core/candidate_filter.py`、`core/query_parameters.py`，不改它们 |
| 主线程 | GUI 接线、CLI、`TASK_PLAN`/`LOG`、合流与验收 |

## 4. 完成定义

两 lane 各自引擎与测试通过、只读、无 GUI 改动；主线程合并后做 GUI 接线并跑全量矩阵与三个 Tk 模块；只读增量复审无 P1/P2。
