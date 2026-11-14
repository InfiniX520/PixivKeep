# E1 下一步计划：小样批次、规则版本、按组范围与冲突保留（2026-11-15）

基线 `c10919b`。本文件只做**只读盘点与拆分**，不改任何产品代码、不改测试、不碰 `organize.cmd`。
依据：`docs/refactor/detailed_design/08_FOLLOW_ORGANIZER_EXTENSION.md`（§3 流程、§5 阶段、§6 F01–F10、§8 协议、§9 E1-2 边界）、`ORGANIZER_CLI.md`、`organizer/*`。

## 1. 现状盘点：E1-2 真正具备什么

| 能力 | 证据（文件:行） |
| --- | --- |
| 14 个子命令（browser/connect/status/show/list/snapshot/sample/sample-page/review/plan/apply/reconcile/undo-plan/export） | `organizer/cli.py:29-59` |
| 账号绑定与**每次调用核对**（不串账号） | `cli.py:104-121`、`web.py:75-77`、`snapshots.py:52`、`samples.py:107,111,161`、`plans.py:16,87,102,153` |
| 双可见性盘点 + 续跑 + 显式非原子标记 | `snapshots.py:11-70`、`cli.py:122-125`（`non_atomic:True`） |
| 跨作品 ID 范围取样、默认 16/上限 20、每样本最多 40 图、缓存 512 MiB/单图 8 MiB | `samples.py:16-24,18,119,169,89-103` |
| 审阅必须声明实际看过；至少 12 个不同作品或 `needs_more` | `samples.py:188-216`（:206-210） |
| 计划不可变（`create_only`）、单次最多 20 位画师 | `plans.py:9-28`、`store.py:72-78`、`plans.py:10` |
| 意图先落库 + 状态机（intent/confirmed/not_applied/conflict） | `store.py:90-131` |
| 写回只有"加组/移出"（**没有**改名、删组、取关、改公开性） | `store.py:98-110`、`page_bridge.js:121`（`tag_add`/`tag_delete`） |
| 逐项读回、对账、补偿计划 | `plans.py:70,85,100,151` |
| 预算（80 请求/32 MiB/600 s/0.5 s 间隔）与**与下载器共用锁** | `common.py:92-127`、`cli.py:79-81` |
| 业务路径相对项目根、拒绝链接与越界 | `common.py:60-73`、`cli.py:67-69` |

### 1.1 文档写了、代码没有（带行号，共 7 条）

1. **分类规则只是一段字符串**：`samples.py:191,216` 只把 `rules_version` 抄进审阅记录，没有版本注册表、没有"规则→分组口径"的真值表；`organizer/` 下也没有 rules 模块。
2. **没有"上轮建议 vs 当前网站"的差异分类**：`plans.py:56`（`catalog_after_allowed`）与 `plans.py:85`（`reconcile`）只比对"写回前后"，无法回答"用户自己改组/改名/删组之后，旧计划哪几项还成立"。设计 §3.4 要求这项能力。
3. **没有小样批次编排**：`cli.py:40-42` 一次只能 `--artist` 一位；`plans.py:10` 只限制单次计划 20 位。E1-3 的"十来位画师、含作品少与风格混杂者"需要批次对象、批次进度与断点，现在没有。
4. **没有按组下载范围**：`core/` 里 `follow_tag`/`group_scope`/`memberships_complete` 零命中；`organizer/` 也只写分组，不产出任何下载范围。E1-4 的 F08 完全无从谈起。
5. **没有批次级汇总报告**：`cli.py:56-58` 的 `export` 只导出单份文档；E1-3 的"每位有多图证据与理由、网站结果可查"需要一份批次报告把 sample/review/plan/operation 串起来。
6. **没有五状态进度字段**：`store.py` 只有文档 kind 与 operation state；`TASK_PLAN.json` 里 E1 没有"契约/代码/整链验收/UI 接线/生产验收"的分别记录（DEVELOPMENT_GUIDE §0.5 要求）。
7. **没有服务端分组数量上限的探测**：设计 §2/§8.2 明说未做过压力验证，代码里也没有探测入口——这不是缺陷，但它决定"分组数量上限"只能记 unknown。

（另有一条**不是缺口**但要在 E1-4 之前定：缓存只做总量检查、没有回收路径，`samples.py:89-103`；长期分批运行会一直涨。）

## 2. 离线可做 vs 必须真实账号

### 2.1 我（自动化）绝不执行的真实动作

- 任何联网命令：`connect`/`snapshot`/`sample`/`sample-page`/`plan`/`reconcile`/`undo-plan` 与 `apply --execute`；
- 任何写回：`page_bridge.js:121` 的 `tag_add`/`tag_delete`；
- 任何真实账号盘点、真实图库读取、真实 Cookie/CSRF 触碰。

这些只能由用户在自己已登录的浏览器里、由用户自己执行。自动化只交付**可离线验证的代码与合成验收**。

### 2.2 可以完全离线做完的（本轮范围）

| 项 | 为什么离线可验 |
| --- | --- |
| 批次编排与断点（谁、到哪一步、预算、下次从哪继续） | 纯状态机，输入合成 store |
| 规则版本注册表与分类真值表（作品层/页层/未知） | 纯数据 + 纯函数 |
| 按组下载范围（分组名→快照成员→不可变范围→多组重叠去重） | 读快照即可，不联网；范围对象是纯数据 |
| 冲突与人工修改保留（两份快照 + 一个计划 → 差异分类 → 决策） | 纯函数，输入两份快照 |
| 批次汇总报告（消费上面四项的结果） | 纯渲染 |
| 撤销计划的生成与核对（离线部分） | 已有 `undo_plan`，用假 web 与合成 store 测 |

## 3. 冻结契约（每个字段：生产者 → 消费者 → 未知怎么写）

### 3.1 批次 `Batch`

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `batch_id, account_id, created_at` | 批次模块 | 报告、CLI 列表 | 必填，缺即拒绝 |
| `scope{snapshot_id, group(可空), artist_ids[], rules_version}` | 批次模块（从快照取，不猜） | 编排队列 | 快照里没有的分组 → 拒绝建批次（不许"大概这些"） |
| `policy{count_per_artist, max_artists, budget}` | 调用方（CLI 参数） | 取样与预算 | 缺 → 用设计默认（16/20/80 请求）并在报告里注明 |
| `steps{artist_id: pending\|sampled\|reviewed\|planned\|applied\|verified\|skipped}` | 批次推进器 | 报告、续跑 | 没做的一律 `pending`，**不许**用 0 或推导值填 |

**可测形式**：把一个批次跑到 `sampled` 后中断，重建批次对象，`steps` 与断点必须逐项相等；对同一 artist 重复推进必须是幂等（不产生第二个 sample）。

### 3.2 分类规则 `ClassificationRule`

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `version, dimensions[], group_names[], created_at` | 规则模块 | 审阅、批次、报告 | 没有规则 → 该画师 `needs_more`，不进入计划 |
| `decisions{artist_id: {groups[], basis, viewed_works, low_confidence}}` | 审阅导入 + 规则套用 | 计划生成 | 证据不足 → `low_confidence=True`，不进计划 |

**可测形式**：同一 `rules_version` 下，同一份审阅 + 同一份快照必须产出逐字节相同的决策表；`rules_version` 变了必须能列出"哪些画师的分组会变"，但不许自动改写已确认的网站结果。

### 3.3 按组下载范围 `GroupScope`

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `snapshot_id, groups[(visibility, name)], artist_ids[], source{visibility, catalog_complete}` | 范围模块（读快照） | 下载任务创建 | 快照 `catalog_complete=False` → 范围标 `partial`，不许当全集 |
| `fingerprint`（成员集合的规范摘要） | 范围模块 | 任务、续跑、审计 | — |
| `completeness ∈ complete\|partial\|unknown` | 范围模块 | 任务创建 | 依据快照的完整性；快照没记录 → `unknown` |

**组身份是 `(visibility, name)` 而不是名字**：公开关注与非公开关注是两个独立命名空间，同名不代表同一组。因此：调用方传纯名字时必须在整份快照里唯一命中一个组，跨侧同名一律 `group_ambiguous`（不猜、不合并）；显式传 `(visibility, name)` 才可取该侧成员，且 `fingerprint` 必须把 visibility 算进去（同名不同侧的两个范围指纹必须不同）。

**可测形式（"按组下载范围不可变"）**：任务创建时保存 `fingerprint`；之后对同一分组再取快照，`ensure_current(scope, snapshot2)` 若成员不同必须抛 `scope_changed`，由调用方**新建**任务，而不是把旧任务的范围悄悄加宽。多组重叠时按 `artist_id` 去重，并记录每个 artist 的来源组。

### 3.4 冲突 `ConflictDecision`

| 字段 | 生产者 | 消费者 | 未知时 |
| --- | --- | --- | --- |
| `artist_id, kind ∈ unchanged\|moved\|group_renamed\|group_deleted\|new_member\|artist_gone\|unknown` | 冲突模块（两份快照比对） | 批次续跑、报告 | 无法判定 → `unknown`，一律不写 |

> `artist_gone` 与 `unknown` 的区别是**已知与未知**的区别，不能合并：快照完整、名单读得到、画师确实不在关注列表里 → `artist_gone`（已知事实，动作 `needs_human`，绝不自动删计划条目）；快照不完整、名册分区读不出、坏行 → `unknown`（判断不了）。把已知说成未知，与"读坏了当空组"是同一类错误。
| `evidence{before[], after[], observed_at}` | 同上 | 人工复核 | 缺任一侧 → `unknown` |
| `action ∈ keep\|replan\|needs_human` | 决策表 | 批次（决定跳过谁） | 默认 `needs_human` |

**可测形式（"人工改组后续跑不覆盖"）**：给定"上轮建议 A→组1"，用户在网站上把 A 移到组2——续跑必须产出 `kind=moved, action=keep, needs_human=True`，且**不生成任何回写操作**；用户删掉的组不得被自动重建；`undo_plan` 不得把用户改过的名字再改回去。

## 4. 五路拆分（文件互斥，每路只新建自己的文件 + 自己的测试）

| lane | 独占文件 | 离线验收用例（要点） |
| --- | --- | --- |
| **L1 批次** | `organizer/batches.py`、`tests/test_organizer_batches.py` | 建批次→跑到 sampled→中断→重建后逐项相等；重复推进幂等；`steps` 缺项保持 pending；预算耗尽后状态为 `paused` 且断点可续 |
| **L2 规则** | `organizer/rules.py`、`tests/test_organizer_rules.py` | 同版本同输入逐字节相同输出；版本变更能列出"会变的分组"；证据不足 → `needs_more`；不允许把 `viewed_works` 少于阈值当成已覆盖 |
| **L3 按组范围** | `organizer/group_scope.py`、`tests/test_organizer_group_scope.py` | 分组名→成员；重叠多组去重且记录来源组；`catalog_complete=False` → `partial`；`ensure_current` 在成员变化时抛 `scope_changed`；指纹与顺序无关 |
| **L4 冲突** | `organizer/conflicts.py`、`tests/test_organizer_conflicts.py` | 六类 kind 各一条；用户改组→keep+needs_human 且零回写；删组不重建；两侧缺一侧 → unknown；决策表穷尽（每个 kind 都有 action） |
| **L5 报告** | `organizer/report.py`、`tests/test_organizer_report.py` | 批次报告含每位画师的取样/审阅/计划/写回状态与未知项计数；未知印「未知」不印 0；JSON 与文本两条出口；报告不含凭据字段 |

约束：五路**互不 import**（只经本文件冻结的字典形状），不联网、不写 `organizer_data` 真目录（测试用 `tmp_path`），不碰 `organize.cmd`/`organize.py` 的既有子命令语义。

## 5. 明确不做 / 只能是 unknown 的

**不做**：真实写回（tag_add/tag_delete 由用户执行）、真实账号盘点与联网、装包或升级、真实图库/凭据触碰、给 `organize.py` 加会联网的新子命令。

**只能记 unknown、不许猜**：
1. 服务端分组数量上限与并发事务语义（设计 §2/§8.2 未验证）；
2. 分组改名后的身份（分组只有名字，没有稳定 ID）；
3. 网站改名的检测精度（只能表现为目录差异，除非用户提供操作记录）；
4. 真实 571+ 位画师的批量性能与限流行为；
5. 缓存长期累积的真实占用（没有回收路径，也没有真实样本量）。

## 6. 建议的落地顺序

**L4 → L3 → L1 → L2 → L5**。理由：L4 是 E1-3 完成条件里"用户手动改组后不覆盖"的唯一实现，且它是纯函数（两份快照进、决策表出），最容易独立验收；L1 的续跑必须先知道"哪些旧项还成立"（L4 的输出）；L3 独立于批次，可以并行做；L2 依赖 L1 的批次口径；L5 只消费前四者。
