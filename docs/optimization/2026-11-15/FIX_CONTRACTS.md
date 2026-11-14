# 审计修复 S0：四个共同契约（2026-11-15）

起点 `031627c`。审计材料：`.runtime/jpgpng_u1u2_audit_20261115_01/review/docs/audits/2026-11-15-u1u2-independent/`（FINDINGS / DEVELOPMENT_GUIDE）。
独立反例已原样带入本分支：`tests/test_audit_independent_20261115.py`，冻结时 **15 红 / 2 绿**（两个绿是正常对照）。该文件是红基线：**不许改它**，修到全绿为止；它通过了才算契约落实。

本文件只冻结"字段叫什么、谁生产、谁消费、未知怎么写"。没有生产者的一律写 unknown，不许用 0 或另一个单位顶替。

## 1. 临时对象所有权（UX-01 + WR-04）

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `OwnershipClaim{path, identity(volume+file id 或 st_dev+st_ino), parent_identity, created_at, purpose}` | 独占创建（O_EXCL）当场取 `fstat`，**返回已持有句柄 + claim** | 复制/写入、SQLite 使用、清理 | 拿不到身份 → 不创建，报 `ownership_unavailable` |
| `verify_owned(handle, claim) -> owned\|foreign\|unknown` | 所有权模块 | 任何 `truncate`/`write` **之前** | unknown 一律不写 |
| `sidecar_owned` | 只有"本次 SQLite 打开后由 SQLite 创建"或"身份与 claim 相符"才算自有 | 清理 | 无法证明 → **留存**并在回执里点名具体文件与原因 |
| `result{ok, main_error, cleanup_errors[], retained[], claims[]}` | 一次调用的统一回执 | 维护 CLI、验收入口 | 主失败与清理失败**分字段**，`finally` 不得覆盖主失败 |

不许：名字精确就算自有、copy 后重新 `record` 掩盖替换、遇错递归删、把任意异常吞成 `ok=True`。

## 2. 质量观察（UX-02/03/04）

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `content_version`（检查开始时的 size+sha256） | `quality_check.check_file` 在**第一次 I/O 之前**建立 | 组合层、CLI 退出码 | 读不到 → `unknown`，不得判成功 |
| `path_stability`（结束时再观察一次） ∈ `unchanged` / `changed` / `unknown` | 同一个检查函数 | `_decode_outcome` | `changed`/`unknown` **不能成功退出** |
| 五维各带 `source` | 各维度自己 | 收据、文案 | `source=none` 时该维必须是 `capability_unavailable`/`undecided`，不得 `ok` |
| `transport.length_matched` | 只在"预期长度与实际长度相等"时 | 收据 | 无预期或不等 → `length_mismatch`/`length_unknown` |
| `budget.scope` | `decode_limits` 与调用方 | 全部阶段 | 作用域是**整条命令**：第一次 I/O 前检查 stop，字节/时间在阶段之间也检查 |

不许：`expected_size is not None` 就写匹配、旧字节通过当当前路径通过、只给最后解码阶段上预算。

## 3. 覆盖事实（UX-05/06）

DTO v1（入口规范化，冲突即拒）：

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `run.totals.artists_total` | `gather_facts`（把仓库的 `total_artists` 规范化成这一个名字） | `coverage_counts` 分母 | 缺 → `None` |
| `artists[].works_seen`（原 `pages` 更名，语义=该画师看到的**作品**数） | `gather_facts` | 报告里的 `counts.artworks_seen` | 缺 → `None` |

> 词表说明（2026-11-15 合并时确认）：**DTO 字段名与报告键故意不同名** —— 事实层叫 `run.totals.works_seen` / `artists[].works_seen`，报告层叫 `counts.artworks_seen`。理由是红基线 UX-05 已经断言了报告键 `artworks_seen`，报告词表服从已发布的验收；别名只在入口规范化，两种拼写同时出现且值不同即拒绝。
| 作品层：`works_seen / excluded_by_filter / works_deliverable / works_unknown` | counts | 文案、完成判定 | 各自 `None` |
| 页层：`pages_expected / pages_saved_new / pages_already_verified / pages_failed / pages_pending_verify` | counts（需要页集证据） | 完成判定 | 无页集 → `pages_expected=None` → 交付只能 `unknown` |

单位铁律：**作品 ≠ 图片页**。不许用 `works_seen - excluded <= pages_saved` 宣布完成，也不许要求 `works_seen == pages_saved + excluded` 来判自洽（一作品多页时天然不等）。
交付 `complete` 需要：当前可见范围明确 **且** 该范围内每个应保存页都有匹配内容版本的有效证据。只给到 crawl 事实时（没有页集与内容版本），交付保持 `unknown`（reason `page_evidence_missing:pages_expected`）。

**唯一的例外**：当前可见范围被明确证明为空（关注列表为空、`no_artists`）时，"每个应保存页都有证据"对空集成立，可以 `complete`。这是空集语义，不是"证据缺失也能 complete"的捷径，两侧都有测试。

## 4. 依赖判断（UX-07）

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `inputs.declared{ok,errors[]}` / `inputs.imports{ok,unparsed[]}` / `inputs.environment{ok,pip_check_exit}` | 三个采集器 | 组合器总判定 | `ok=False` 必须参与 verdict |
| `constraints[]`（规范化名 + 全部适用 specifier + 来源文件/行 + group + markers/extras） | 声明解析 | 矩阵 | 保留**全部**适用约束，不许首条赢；runtime 不被 dev 覆盖，但约束不因分组被丢弃 |
| `verdict` ∈ `ok` / `attention` / `unknown` | 矩阵 + 组合器 | `exit` | 已知冲突与未决可同时出现；`unknown` 不得掩盖 `attention` |

退出码：`0` 只有"输入完整 + 全部约束满足 + pip check 通过"；`3` 输入不完整 / 未执行；`2` 有冲突或缺失。解析失败只写进 notes 不算处理过。

## 5. 文件所有权（并发纪律）

| 工作包 | 独占文件 |
| --- | --- |
| S1 所有权与错误协议 | `core/workspace_ownership.py`、`core/library_maintenance.py` 的建立/复制/SQLite/清理生命周期 |
| S2 质量引擎 | `core/quality_check.py`、`png_decode.py`、`decode_limits.py`、`decoder_registry.py`、`quality_evidence.py` |
| S3 覆盖事实 | `core/follow_facts.py`、`follow_coverage*.py` |
| S4 依赖报告 | `tools/maintenance/dependency_*.py` |
| 集成者（主线程） | `core/library_maintenance.py` 的 `_decode_outcome` / `_decode_lines` / `decode-check` 出口、跨包接线、全量验收 |

S2 不碰 `library_maintenance.py`；CLI 出口由集成者在 S1 合并后串行改。合并顺序 S1 → S2(+出口) → S3 → S4。

## 6. 验收门槛（缺一不算完成）

1. `tests/test_audit_independent_20261115.py` **17/17**（原样，不改断言）；
2. 上一轮 14 条原样反例 14/14；归档工具 23/23；
3. 全量 `tests/` 0 failed / 0 errors（除已登记的两个环境 flake：Tk 初始化、log-view 10 ms 刷盘竞态）；
4. 真实库、图库、账号、`record_backups` 一个字节都没动；没有装包、没有改共享解释器。
