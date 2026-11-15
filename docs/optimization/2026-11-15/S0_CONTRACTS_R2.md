# S0 契约冻结（R2：EA-01…EA-09）

日期 2026-11-15。产品锚点 `5661d8b`（fix/a1-u1u2-audit-20261115），审计提交 `75f6ad6`（audit/u1u2-e1-independent-20261115），修复分支 `fix/a1-u1u2-e1-audit-20261115`。

本文是**契约**，不是完成回执。每一项都要另外给出「契约 / 代码 / 整链 / 接线 / 生产」五类证据（见 §12），任何一类缺失都不得在 README 或 IMPLEMENTATION 里写「完成」。

红基线：`docs/audits/2026-11-15-u1u2-e1-independent/evidence/test_boundaries.py` **逐字节冻结**；本分支首次运行为 15 项 = 14 失败 / 1 通过（`verify/red_baseline_01.xml`，与审计 `probes01`/`packaged02` 的 14/1 一致）。修复不得修改该文件，关闭证据写在新文件里。

## 0. 本机实测：写入保护的前提事实

`.runtime/jpgpng_u1u2_e1_fix_20261115_01/probe/probe_write_locks.py`，共享解释器、临时目录、同一卷，无真实图库：

| 操作 | 实测结果 |
| --- | --- |
| 持 `os.open(path, O_RDWR)` 句柄时 `os.unlink(path)` | 拒绝，PermissionError/32 |
| 持句柄时 `os.replace(other, path)` | 拒绝，PermissionError/5 |
| 持句柄时 unlink + 硬链接他人文件占名 | 拒绝，PermissionError/32（**替换本身失败**） |
| 持句柄时 `sqlite3.connect(path)` 写入并 `PRAGMA journal_mode=DELETE` | 成功 |
| 持句柄时 `src.backup(dest)` 写进另一个持句柄的空文件 | 成功 |
| 关闭句柄后 `os.unlink(path)` | 成功 |
| 持目录句柄（CreateFileW + `FILE_FLAG_BACKUP_SEMANTICS`，共享读/写、**不**共享删除）时 `os.rename(dir)` | 拒绝，WinError 32 |
| 同上期间在目录内创建/删除子文件 | 成功 |
| 目录句柄带 `FILE_SHARE_DELETE` 时 `os.rename(dir)` | 成功 |

由此得到两条工程事实，后面的契约都建立在它们上面：

1. **持文件句柄 = 阻止替换，同时允许 SQLite 按名写入。** 因此「打开 → fstat 校验 → 保持到写完」是真正的写能力，而不是「先 stat 再 connect」。
2. **目录句柄只能覆盖一小段。** 它挡住目录自身的重命名，却挡住 `rmdir`；所以只能在「创建/写入成员」期间持有，不能全程持有。Windows 上 `os.open` 打不开目录，父目录固定用 `CreateFileW` 实现；取不到时必须在结果里写 `parent_pin=degraded`，不得静默降级。

## 1. 契约一：写入能力（EA-01）

| 项 | 冻结值 |
| --- | --- |
| 授权对象 | `TemporaryWorkspace.adopt()` 记录的目录身份 `_key`；`claim()` 用 `O_CREAT\|O_EXCL` 创建并 `fstat` 记录的成员身份 `OwnershipClaim.identity` |
| 保护窗口 | 从「按名打开成员句柄并 `fstat` 校验」到「写入结束、句柄关闭」。窗口内不得关闭句柄、不得重开按名写、不得把句柄借给第二个写入者 |
| SQLite 用法 | `_online_snapshot` 必须先 `with claim.lease():`，再 `sqlite3.connect(str(target))`。SQLite 只在这段租约内按名打开 |
| 父目录 | `claim()` 与 `lease()` 期间持父目录句柄，并 `GetFileInformationByHandle` 比对 `_key`；不符即 `parent_replaced` |
| 不可用 | 卷不报身份（`key_available` 为假）→ 不认领、不写、`ownership_unavailable` |
| 矛盾 | 名字身份 ≠ claim 身份 → `snapshot_target_replaced` / `claim_not_owned`，**一个字节也不写** |
| 过期 | `created_at` 不是授权；同一 claim 不跨进程、不跨重启复用 |

生产规则：claim 与 lease 之间的窗口允许被替换（审计钩子正落在这里），lease 用 `fstat` 当场发现并拒绝；lease 之后的替换由系统直接拒绝（§0 第 3 行）。两者合起来才叫闭合，缺一不可。

旁车（`-wal`/`-shm`/`-journal`）：只对自己 claim 并写完的名字声明所有权；外部旁车一律保留并命名，不得「看见同名就认领」。`PRAGMA journal_mode=DELETE` 之后仍出现非空 `-wal`/`-shm` → `snapshot_not_self_contained`，退出非 0，文件保留并报路径。

## 2. 契约二：观察预算（EA-02）

| 项 | 冻结值 |
| --- | --- |
| 单位 | **一次 `check_file` 调用 = 一个文件**。`budget.scope` 写 `file_call`；多文件 CLI 必须另外打印 `files=N` 与逐文件预算之和，**不得**把单文件预算标成整条命令总额 |
| 阶段 | `observe` → `version_before`(hash) → `structure` → `decode` → `version_after`(hash) → 判定 |
| 读点计费 | 每次 `read(n)` 之前 `n = min(n, remaining_bytes)`；读后立刻 `charge(实际字节)`；`charge` 返回 `bytes_limit` 时该阶段立即停止 |
| 取消/时间 | 每块（≤256 KiB）检查 `stop.is_set()` 与 `check_time()`；已发出的有界 read 允许完成，禁止接着读完剩余文件 |
| 解码回传 | `DecodeReceipt.bytes_read: int \| None`，由解码器在读取点累计。缺字段不得兜底成 0，而是 `budget.state=unknown`、reason `decoder_did_not_report_bytes`，终态不得成功 |
| 终态 | 预算耗尽 → `path_stability=unknown`、对应 `Dimension(undecided, bytes_limit)`、`_decode_outcome` 非 0。禁止「第二次整文件哈希补证明」 |
| 未知 | 读量测不到 → `bytes=None`，CLI 打印「未知」，不写 0 |

## 3. 契约三：页完成（EA-03）

| 项 | 冻结值 |
| --- | --- |
| expected | `page_identity → content_digest \| None`（映射，不是集合） |
| observed | `page_identity → {state, digests: set[str]}`；只有 `saved_new` / `already_verified` 可参与证明 |
| 完成判据 | 该页 state 合格 **且** observed 的 digest 恰有一个等于 expected digest |
| 期望版本缺失 | `content_digest is None` → 该页 `unknown`，`delivery.state != complete`，reason `expected_content_version_missing` |
| 冲突 | 同页出现多个不同 digest → `page_content_conflict:<n>`，该页 `needs_verification`，`delivery` 不得 complete |
| 重复 | 同版本重复观察可去重（`duplicates` 计数保留） |
| 权威/顺序 | 不得用列表顺序猜最新；将来若引入 `observed_at`，必须显式持久化并先改本契约 |
| 过期 | 旧 snapshot / 旧 policy 的 receipt 不算完成 |

## 4. 契约四：覆盖数据来源（EA-04）

| 项 | 冻结值 |
| --- | --- |
| V2 主键 | `jobs.job_id` → `batches.batch_id` → `batch_items(batch_id, illust_id)`；`follow_snapshots.snapshot_id`（`origin_job_id`）→ `follow_snapshot_members`、`follow_snapshot_pages(page_identity, content_digest)` |
| 交付事实 | `deliveries(delivery_id, job_id, illust_id, page_index, state, generation)` + `artifacts(delivery_id, sha256, size, verification)`；页接受 `source_batch_pages(batch_id, page_identity, content_digest, delivery_ids_json)` |
| 视图 | 一个报告只用一个只读连接、一个读事务；`mode=ro` + `PRAGMA query_only=1`。禁止 job 读完再另开事务读 batches（混代） |
| 作品分母 | `batch_items` 每 illust 一行（含 `page_count` / `required_pages_json`）；**不得**用交付行数、文件名数量推页数 |
| 页分母 | 只来自 `required_pages_json` 或 `page_count`；缺失 → `pages_expected=unknown` |
| 完整性 | `batches.coverage_complete IS NULL` → `unknown`（不是 0/1）；`follow_snapshots.coverage_complete` 同理 |
| 快照匹配 | `follow_snapshots.origin_job_id != job_id` → `scope_snapshot_mismatch` |
| legacy | `core/crawl_state_reader.py` 保留，但只在显式 `--provider legacy` 或显式 `--state-path` 下使用，输出 `provider=legacy`；默认永远走 V2 |
| UI | `coverage_summary(job_id=...)` 必须带所选任务 id；没有选择 → `job_required`，不让用户去旧库抄 run_id |

## 5. 契约五：冻结范围（EA-06）

| 项 | 冻结值 |
| --- | --- |
| Batch 冻结 | `scope` / `policy` / `steps` **递归**冻结：list→tuple，dict→不可变映射；`as_dict()` 返回全新的 JSON 结构（list/dict），改它不影响对象 |
| Batch 摘要 | `fingerprint()` = 规范化 `{scope, policy}` 的 sha256；文档不得称其为「签名」或「防篡改」 |
| GroupScope 冻结 | 属性只读（构造期 `object.__setattr__`，之后赋值 → `AttributeError`）；`as_dict()` 全新结构；`from_dict()` 校验 `steps/blocks/group_pairs/source/completeness` 与 `fingerprint` 一致 |
| 成员一致 | `blocks` 与 `artist_ids` 不符 → `scope_members_inconsistent`；`blocks` 与 `memberships` 不符 → `scope_membership_mismatch` |
| 未知 | 缺字段 → 稳定类型化错误码，不用默认值补 |

## 6. 契约六：批次续跑（EA-05）

| 项 | 冻结值 |
| --- | --- |
| 三分类 | OPEN = `pending/sampled/reviewed/planned/failed`（可推进）；AWAITING_VERIFY = `applied`（只能核验）；FINAL = `verified/skipped` |
| resume | 返回 OPEN ∪ AWAITING_VERIFY，按批内顺序；`applied` 必须在里面 |
| rebuild | 只接受沿 `TRANSITIONS` **前进**（BFS 可达）的目标态；回退 → `progress_not_forward`；`applied→verified` 合法 |
| status | 每次由 steps 计算：全 FINAL → `complete`；否则 stop_reason 是限额 → `paused`；否则 `active`。不得只在 paused 分支更新 |
| 集合守恒 | `pool = done ∪ planned ∪ remaining`，三者互斥；游标按**实际入选数**推进 |
| 零容量 | `plan_batches` 立即停止：`remaining=全部`、`stop_reason=capacity_zero`，不产空批、不吞名单 |
| 写回未知 | `apply` 已发出但没有读回 → 只能 reconcile/verify，不得重发 apply |

## 7. 契约七：审阅证据（EA-07）

| 项 | 冻结值 |
| --- | --- |
| 两个分数 | `match_strength`（规则权重，只能建议分组）与 `visual_evidence`（看图充分度）分开输出，不得互相冒充 |
| 高置信必要条件 | `match_strength=high` **且** `visual_evidence.state=sufficient`；后者只由 `organizer.samples.review` 的收据产生 |
| 收据要求 | ≥ `min(12, available_works)` 个**不同**作品、样本 SHA 与声明一致、有实际查看声明、`rules_version`/`snapshot` 未过期 |
| 作品去重 | 同一作品的多页只算一个作品；重复页不能凑数 |
| available_works | 只能来自已验证的采集事实；调用方随手填写不得降低门槛 |
| 未知 | 缺 `viewed_works` / 缺 SHA → `visual_evidence.state=unknown`、`needs_more=True`、reason `visual_evidence_missing` |
| 过期 | 旧 `rules_version` 的收据 → `stale_receipt`，不算充分 |

## 8. 契约八：报告输入（EA-09）

| 项 | 冻结值 |
| --- | --- |
| 校验顺序 | Batch → GroupScope → decisions → 组合不变量 |
| 必须成立 | `decisions ⊆ batch.artist_ids`（无决定 → unknown）；`batch.artist_ids ⊆ scope.artist_ids`（`covers_batch`）；`snapshot_id`/`rules_version` 一致或显式声明差异；`account_id` 与 scope 来源一致 |
| 大范围覆盖子批次 | 只在显式 `superset_ok` 且记录包含关系时合法 |
| 退出码 | 0 = 输入合法 + 范围覆盖 + 无 unknowns + scope complete；5 = 输入合法但证据不足；3 = 输入非法/被拒；I/O 失败非 0 |
| 拒绝方式 | `covers_batch=False` → 抛类型化拒绝（exit 3），不是多打一行警告 |
| 永久保留 | `proof_limits.website_write_back` 恒为 `not_proven_here` |

## 9. 文件归属与分波

共享文件先定唯一所有者，禁止两路同时改：

| 波次 | 包 | 独占文件 | 前置 |
| --- | --- | --- | --- |
| 1 | W1 写入授权 | `core/workspace_ownership.py`、`core/library_maintenance.py`（`_online_snapshot`/`backup_library`） | §0、契约一 |
| 1 | W2 观察预算 | `core/quality_check.py`、`core/decode_limits.py`、`core/png_decode.py`（回执） | 契约二 |
| 1 | W3 覆盖引擎 | `core/follow_coverage_counts.py`、`core/follow_coverage.py`、`core/follow_coverage_state.py` | 契约三 |
| 2 | W4 整理核心 | `organizer/batches.py`、`organizer/group_scope.py` | 契约五、六 |
| 3 | W5 审阅与组合 | `organizer/rules.py`、`organizer/report.py`、`organizer/offline_cli.py` | W4 协议稳定 |
| 3 | I 接线 | `core/collection_desktop.py`、`core/report_commands.py`、`core/crawl_state_reader.py`（只加 V2 适配器，不改旧语义）、`gui/coverage_panel.py` | W3 接口验收 |

主智能体自己写：契约、CLI 聚合、文档、最终整链；不把各路单测通过直接求和当集成验收。

## 10. 禁止的捷径

- 不得为了让审计钩子「成功替换」而主动关闭保护句柄，也不得为了让 `hits=True` 削弱锁。
- 不得把 `covers_batch=False` 降级成警告；不得把未知写成 0；不得用列表顺序猜最新版本。
- 不得为通过 EA-02 而 mock 整个 `_sha256`：必须证明真实读流可被取消。
- 不得用 `object.__setattr__` 之外的反射手段证明不可变；反例必须来自普通公共操作。
- 不得把 `applied` 继续留在 FINAL_STATES 里「看起来通过」。
- 不得在没有 V2 事实时用 legacy 数字补分母；缺失就是 unknown。
- 不得修改冻结红基线；不得把未完成复核写成通过。

## 11. 验收矩阵（每项都要有红→绿证据）

| 编号 | 必测 |
| --- | --- |
| EA-01 | claim 后替换目标（哨兵 SHA/大小不变）、connect 前换父目录、写完后替换、external sidecar、connect/backup/close 异常、源库 WAL 活跃、取消、清理拒绝、正常在线备份可 verify |
| EA-02 | 0/恰好/少 1 字节预算；五阶段分别耗尽；每阶段取消与时钟到期；合法/损坏/未知格式；成功与失败计数都与 instrumented read 求和一致 |
| EA-03 | 期望同版本（complete）、仅旧版本（非 complete）、新旧并存（冲突）、期望缺版本（unknown）、重复同版本去重 |
| EA-04 | 空新装、仅 V2 合成数据、仅 legacy、两者并存且结论不同、多 job/批次、读时推进、完整/缺页/旧 SHA |
| EA-05 | pool 0/1/5/21；预算 0/1/小于 max_artists/足够；done 重复与乱序；每态中断后序列化重启；applied→verify |
| EA-06 | 导出对象修改不影响原对象；构造入参修改不影响；嵌套 policy 修改不影响；GroupScope 赋值拒绝；坏形状 from_dict 拒绝 |
| EA-07 | 零作品、重复同作品多页、11/12 个独立作品、已证实只有 3 个作品的画师、unknown available_works、样本 SHA 不符、旧 rules_version |
| EA-08 | memberships 与 artist 行不符（complete 标志仍为真）、同成员但完整性降级、visibility 不同、正常完整快照往返 |
| EA-09 | 范围不覆盖（拒绝）、范围更大（显式 superset）、decision 越界、坏形状/矛盾字段类型化拒绝、退出码 0/5/3 各自可达 |

## 12. 五种状态与不变量

每个发现必须分别给出：**契约**（本节）、**代码**（实现 + 定向用例）、**整链**（合成端到端）、**接线**（UI/CLI 真实入口）、**生产**（另批，本轮不做）。只写「已实现」一种状态视为文档过度声明。

不变量（每次提交都要能回答）：

1. 失败的命令不得退出 0；未知不得写成 0。
2. 只读路径必须可证明只读（sha256 + mtime + 目录清单不变，无 `-wal`/`-shm`）。
3. 冻结文件（红基线、审计报告、已发布检查点）不得被修改。
4. 每个修复都要有「修复前必红」的证据，且该证据在修复后仍可复现为绿。
5. 用户可见行为变化必须在同一提交里写进文档。
