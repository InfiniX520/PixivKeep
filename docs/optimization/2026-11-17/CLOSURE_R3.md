# 944ee13 复审六项的关闭报告（第三轮）

锚点：被审候选 `944ee13`，审计分支 `audit/u1u2-e1-followup-20261117` @ `5d1e676`，
修复分支 `fix/a1-ra-20261117`。契约在动手前冻结：[S0_CONTRACTS_R3.md](S0_CONTRACTS_R3.md)。
反例文件 `docs/audits/2026-11-15-u1u2-e1-r2-independent/evidence/test_adjacent.py`
（sha256 `7c7914066dca4166cb1eccd75b6e0ddf2dcb2dbe3abe8d1ef6a7b7a9ac7c0214`）逐字节未改。

## 结论

六项各自按契约实现并有红→绿证据，但**不是"六项全部闭合"**：

- 你报的 13 条相邻反例现在是 **12 通过、1 失败**；失败的那一条（RA-04 反例 #1）与同文件的反例 #2
  对同一输入要求相反结论，本轮按契约 §5 与 §10.1 的裁决让它停在第一个断言，其目标由新增用例承接。
- **生产面一律未做**：真实账号写回、真实图库/备份恢复、云盘往返、真实库长跑都没有验收，
  所以本报告不写"已发布""已上线"。
- 六个包只修"证据缺口"，没有新功能；V2 缺作品页期望内容代次这个数据面问题仍然存在（见 §9）。

## 逐项状态

| 编号 | 契约冻结 | 实现 | API 整链 | UI/CLI | 真实生产 |
| --- | --- | --- | --- | --- | --- |
| RA-01 P1 SQLite 文件族授权 | 已冻结（§1） | 已提交 `25836ef` | 已验（`backup→verify`、`file_group`） | 已验（`maintain backup` 文本+JSON） | 未做 |
| RA-02 P2 严格只读/活跃库读取 | 已冻结（§3，按实测改写 §10.2） | 已提交 `0cf13de` | 已验（CLI + DesktopRuntime 两条路径） | 已验（`coverage` 文本加"读取方式"行） | 未做 |
| RA-03 P2 V2 关系取数 | 已冻结（§4） | 已提交 `55a7cf7` | 已验（GUI 与 CLI 数字相同） | 已验（未知分母有专门一行） | 未做 |
| RA-04 P2 canonical review 与消费门禁 | 已冻结（§5，§10.1 裁决） | 已提交 `0764f1d` | 已验（真实 `samples.review` → sufficient/high） | 未接真实写回 | 未做 |
| RA-05 P2 账号身份沿 scope→report | 已冻结（§6） | 已提交 `3d208ba` | 已验（`scope-plan`→JSON→`batch-report`） | 已验（退出码 0/3/5 与文本） | 未做 |
| RA-06 P2 解码回执绑定内容 | 已冻结（§2，按 W2 边界改写 §10.3） | 已提交 `901600c` | 已验（CLI `decode-check` 退出码与 JSON 一致） | 已验（文本新增"内容: 已绑定/未绑定"） | 未做 |

## 反例矩阵（13 条，同一命令、同一文件）

| # | 反例 | 修前 | 修后 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `test_ra01...[-journal]` | 失败 | **通过** | 外来 journal 在新目录结构里从未被 SQLite 打开 |
| 2 | `test_ra01...[-wal]` | 失败 | **通过** | 同上；哨兵字节与 mtime 不变 |
| 3 | `test_ra01...[-shm]` | 通过 | 通过 | 对照 |
| 4 | `test_ra02_wal_without_shm...` | 失败 | **通过** | 私有副本，源目录名字/摘要全同 |
| 5 | `test_ra03_..._snapshot_members_without_items` | 失败 | **通过** | 画师总体 2 |
| 6 | `test_ra03_reused_cross_job_page_is_visible` | 失败 | **通过** | artifact-only 跨任务复用计入 |
| 7 | `test_ra03_one_work_remains_one_work_after_resume` | 失败 | **通过** | works_seen 2→1 |
| 8 | `test_ra04_..._cannot_be_reused_under_new_rule_version` | 失败（第 65 行） | **仍失败（第 62 行）** | 见 §10.1：它断言的是门禁之前的行为 |
| 9 | `test_ra04_..._requires_a_stored_review` | 失败 | **通过** | 未落库的字典永不 sufficient |
| 10 | `test_ra05_scope_keeps_account_identity...` | 失败 | **通过** | 账号不同直接拒绝 |
| 11 | `test_ra06_decode_receipt_is_bound...` | 失败 | **通过** | 解码通道摘要与前后版本摘要不一致即降级 |
| 12 | `test_control_classification_with_missing_visual...` | 通过 | 通过 | 对照 |
| 13 | `test_control_applied_progress_cannot_roll_back...` | 通过 | 通过 | 对照 |

反例 #8 的位置变化（65 → 62）本身就是证据：修前它失败在"v1 证据撑起 v2 规则集"，
修后它失败在"store 里没有这份 review"，即门禁生效。我们**没有**把它写成已通过。

## RA-01 · SQLite 目标文件族的写入授权（P1）

- 位置：`core/workspace_ownership.py`、`core/library_maintenance.py` 的 backup 段。
- 修法：产物在本次运行独占的 `.staging-<id>` 目录里由真实 SQLite 生成；成品目录只由一次 `os.rename` 出现，
  SQLite **永远不会打开以成品名命名的数据库**。连接前预检 `-journal/-wal/-shm` 三个名字，任一存在即拒绝并保留；
  关闭后复核 staging 里只允许本次 claim 记录的名字；发布前复核外来名；`os.rename` 撞到已存在的成品即失败。
  staging 目录用 `icacls` 收紧 ACL（仅当前用户 + SYSTEM），失败记 `acl=degraded` 并进 manifest。
  禁止 `journal_mode=OFF/MEMORY`、`synchronous=OFF` 等削弱手段。
- 已证明：三种旁车在连接前被拒（拒绝时对 staging 路径的 SQLite 连接数为 0）、哨兵字节/时间/硬链接不变；
  外来名阻止发布且原样保留；成品被抢占时 `backup_target_exists` 且对方目录逐文件 sha256 不变；
  干净失败不留空壳；成功发布后 backups 根无 `.staging-*`；发布后目录仍可读写；`backup→verify` ok；
  `file_group` 同样预检。新增 13 条用例；合流后我重跑备份相关 11 个模块 239 项（1 skip 为既有平台用例），
  lane 自报的更大扫描（13 模块 262 项）作为参考列在它的报告里。
- 仍未知（比初稿写得更重）：预检→connect 之间若同用户进程监视私有目录并注入文件，Windows 上没有"拒绝创建子项"的锁。
  发布闸门只能看见**仍然存在**的外来名字；若 SQLite 在打开/关闭时把注入的 `-journal`/`-wal` 删掉（R2 审计实测过这个行为），
  闸门就无可拒、也无从知道它来过。所以这一窗口的正确说法是"**无法证明与无法阻止**"，不是"能被事后拒绝"。

## RA-02 · 活跃库读取与严格无副作用读取（P2）

- 位置：`core/follow_facts_v2.py` 连接层、`core/report_commands.py`。
- 修法：`read_plan()` 按**实测**选择读取方式并把证据随连接返回（`mode/sql_mode/promise/sidecars/strict_no_side_effects/copy/evidence`）。
  选择表按 lane C 的真 SQLite 探针改写：rollback 无热 journal → 直读；真热 journal → 拒绝 `source_hot_journal`；
  WAL 有 `-wal`+`-shm` → 直读（只承诺名字不变，`-shm` 锁字节会变）；WAL 缺任一旁车 → **私有副本**
  （实测：直读会新建 `-shm`；干净关闭的库还会新建 `-wal`(0 B) 与 `-shm`(32 KiB)）。
  `immutable=1` 只能由调用方显式声明冻结副本。报告新增 `read` 与 `facts_notes.read_*`，`coverage_lines` 加"读取方式"一行。
- 已证明：选择表的**每一行**都在 `tests/test_ra02_read_plan.py` 里有直接断言（模式、承诺、旁车事实）；其中
  **副本一行**另有 CLI 与 DesktopRuntime 的端到端用例（`tests/test_ra02_read_plan.py` 的两条，源目录前后名字/摘要不变）；
  其余行没有 CLI 端到端用例——它们由计划层断言 + `coverage` 命令走同一条 `read_only_connection` 覆盖，不夸大成「每行都端到端」。
  源文件族名字/摘要/属性前后比较；副本前后 (size, mtime_ns) 与名字集合比对，任一变 → `source_changed_during_copy`；
  副本目录在关闭与异常路径都清理；活写者并发提交不被误报 `database_changed_during_read`（该码只属于显式 immutable 读）。
  旧 crawl 状态库也改走同一读计划（`core/report_commands.coverage_report`），实测 4 条：干净关闭的 WAL 库走副本且目录不变、
  rollback 库直读且不新建文件、缺库与缺表各有类型化拒绝。
- 代价（实测）：150 MB WAL 库干净关闭，副本 0.160 s、查询 0.001 s、删除 0.010 s；活 WAL 直读 0.002 s + 0.012 s，复制 0 字节。
- 仍未知：副本一致性用 (size, mtime_ns) 前后比较，不做源侧第二次全读——能逃过这一层的 ABA（同尺寸改写并把 mtime 精确恢复）
  不在防护范围；副本需要约一个库大小的临时空间。

## RA-03 · 按 V2 关系重建覆盖事实（P2）

- 位置：`core/follow_facts_v2.py`（`coverage_facts`）、`core/follow_coverage_text.py`、`core/report_commands.py` 的 notes。
- 修法：job → 批次 → `events(kind='follow_snapshot_selected')` → `follow_snapshots`/`follow_snapshot_members` →
  `batch_items` → `batch_item_pages` → `deliveries|artifacts`。画师总体三分支（有完整快照 → 成员数；
  快照不完整 → unknown；非 follow 任务 → `snapshot_scope_not_applicable`；无事件 → `snapshot_selection_not_recorded`），
  **None 不写 0**。无作品画师以 `works_seen=0` 出现；同 job 按 `illust_id` 去重；批次次序 `created_at, batch_id`；
  observed 沿 `batch_item_pages` 解析并允许跨 job 的 artifact-only 复用（`origin_job="other"`）。
- 已证明：三条反例；去重前后 `works_seen` 2→1、expected 2→1、`works_multi_batch` 0→1；跨任务复用 observed 0→1；
  两代异摘要 → `page_content_conflict` 且交付不 complete，同摘要折叠成一条；1k 作品 9 条 SQL/0.081 s/2.0 MiB，
  10,001 作品 9 条 SQL/0.432 s/8.4 MiB（语句数只随批次分块增长）；GUI 与 CLI 的 scope/总数/原因相同。
- 仍未知：V2 没有作品页的期望内容代次，交付维度继续 unknown（见 §9）；`artworks_total` 仍是 None 且文本静默（同一形状，本轮不动）。

## RA-04 · canonical 审阅记录与消费时版本门禁（P2）

- 位置：`organizer/rules.py`、`organizer/samples.py`。
- 修法：`review_evidence` 以 `receipt['id']` 读已落库 review，逐身份字段比对（`review_not_stored` / `receipt_copy_mismatch:<双方摘要>`）；
  `VisualEvidence` 增加 `review_id`/`rules_fingerprint`，`sufficient` 额外要求 `review_id`；
  `classify`/`changed_by_version` 消费时核对 `rules_version` 与 `ruleset_fingerprint`，不符转 stale；
  `samples.review` 记录指纹（接受 `RuleSet` 或 64 位十六进制），旧记录缺指纹 → unknown（`visual_rules_unbound`），不补写历史。
- 已证明：19 条新用例（未落库永不 sufficient、同 id 篡改被拒且不重写、真实 `samples.review` → sufficient/high、
  多页折叠、`available_works` 未知、快照更新、v1→v2、同版本标签不同内容、`as_dict/from_dict` 往返、`changed_by_version` 同门禁）；
  既有 W5 两模块 45 项与 organizer 全量 370 项无回归。
- 仍未知：`organizer/plans.py:16` 的建计划入口仍只看 `account_id`/`needs_more`，不校验规则指纹（见 §9）。

## RA-05 · 账号身份沿 scope → report（P2）

- 位置：`organizer/group_scope.py`、`organizer/report.py`、`organizer/offline_cli.py`。
- 修法：`GroupScope.account_id` + `source.account_id/account_id_state/identity_fingerprint`，`as_dict/from_dict` 往返保留；
  `build_report` 先判身份再判包含：账号已知不同 → `account_scope_mismatch`；账号未知 → `unknowns` 追加
  `scope_account_unknown`、`covers_batch` 照算但整体不得 complete、CLI 退出 5；`--account` 只核对调用上下文；
  `ensure_current` 五态（放行/拒绝/旧 dump 证据变强放行/快照失账号拒绝/双未知放行）。
- 已证明：37 条新用例 + 既有 12 模块 388 项无回归；退出码与文本一致；`scope-plan` 写 JSON 再 `batch-report` 读回整链。
- 用户可见变化：`scope-plan` 无账号 0→5 并打印"账号: 未记录"；`batch-report` 范围账号未知退 5 并多一行；
  `report_version` 2→3，新增 `scope.account_id`、`proof_limits.account/complete/incomplete_reasons`、`provenance.scope_account_id`。
- 仍未知：真实账号快照未验；`REPORT_VERSION 3` 没有外部消费者可验。

## RA-06 · 解码回执绑定真正解码的内容（P2）

- 位置：`core/quality_check.py`；集成人接线：`core/library_maintenance.py` 的 `_decode_outcome`/`_decode_lines`。
- 修法：解码器不再拿到路径，而是一个内容通道——它读的每个字节边读边哈希计数，读完（解码器可能停在 IEND）
  再由同一预算把剩余字节读完，得到整文件摘要。报告新增 `content{state,sha256,bytes,complete,source,reason}` 与
  `content_bound`；四类观察（版本前、结构走查、解码通道、版本后）任一相矛盾即把解码降为 `undecided`。
  `_decode_outcome` 显式要求 `content_bound`，`_decode_lines` 打印"内容: 已绑定 <摘要前 12 位>（读取 N 字节，整文件）"或"未绑定（原因）"。
- 已证明：12 条新用例（A→B→A 同尺寸同 mtime、原地写、路径替换、三阶段分别取消、预算恰边界、`bytes_read` 未知、
  解码器未读通道、补读计费一次、CLI 退出码与 JSON 一致）+ 6 条集成接线用例；W2/W3 47 项无回归。
- 仍未知：结构走查仍按路径打开（靠摘要比对发现差异，不是钉句柄）；核验结束后其他进程继续改文件不在承诺内；
  非 PNG 的第三方解码器当前不存在（`capability_unavailable`，无流可绑）。

## 集成人额外改动（不属于六包，但由合流暴露）

1. `_decode_outcome` 显式检查 `content_bound`：契约 §2 的规则不能只依赖 decode 状态降级（`2c46206`）。
2. `coverage_report_v2` 的 `facts_notes` 增加 `artist_cohort`/`snapshot_*`/`page_links`/复用说明（`866589a`）。
3. `backup_library` 记录 `source_sidecars_created`：实测"只读打开 WAL 库会让 SQLite 在源库旁新建空 `-wal` 与 32 KiB `-shm`"，
   库字节不变但目录清单会变；现在这个差异被点名，且**不**由备份自行删除（`2c46206`，3 条用例）。
4. `core/collection_desktop.py` 的 docstring 把"只读"拆成 `sql_read_only` 与 `source_unchanged` 两个承诺（`de0c30b`）。
5. 旧 crawl 状态库的覆盖命令也接上读计划（`2aa18c3`）：干净关闭的 WAL 库改走私有副本，文本不再笼统说"只读"，
   改由 `facts_notes.read_mode/read_promise/read_note` 与"读取方式"一行说明。
6. 质量通道的预算顺序修正（`2aa18c3`）：解码器读的字节先计费，`finish()` 再补读剩余部分。实测旧顺序在 300 字节上限下
   读到 400 字节并仍把内容标成 observed；修后总账停在上限、内容标为不完整。新增 `tests/test_ra06_budget_ceiling.py`，
   用"同一进程里并排加载 HEAD 版本模块"证明它红→绿（不是靠换工作树文件）。

## 契约修订（都在实施前/实施中，见 S0_CONTRACTS_R3 §10）

- §10.1：RA-04 两条反例互斥，按 §5 实施，#2 绿、#1 停在第一个断言。
- §10.2：§3 选择表按真 SQLite 实测改写（WAL 无旁车 → 私有副本；副本默认允许）。
- §10.3：§2 判定式按 W2 的预算边界改写（"没有观察"不等于"观察到相反"）。

## 验证矩阵（代码与测试的最后一次提交是 `2aa18c3`；矩阵在其后运行，命令见 `verify/run_round.py`）

```
整树（排除 6 个 Tk 模块）  3558 tests / 3553 passed / 0 failed / 0 errors / 5 skipped   verify/final2_body.xml      F8E7F7CE8C993E07
Tk 四模块                 118 tests / 118 passed / 0 failed / 0 errors / 0 skipped    verify/final2_tk.xml        F23BF72D98EFB1EF
两个 presentation 模块      9 tests / 9 passed / 0 failed / 0 errors / 0 skipped        verify/final2_pres.xml      4C8058267E3F4A55
冻结红基线（旧 15 项）      15 tests / 15 passed                                     verify/final2_frozen.xml    FBCE4F6DF6A722FD
相邻反例（新 13 项）        13 tests / 12 passed / 1 failed（RA-04 #1，见 §10.1）        verify/final2_adjacent.xml  330532029CB153C1
修复前基线（同一 13 项）    13 tests / 3 passed / 10 failed                            verify/ra_before.xml        846B7412EBB7F985
```

合计 **3685 项（3558 + 118 + 9）**：**3680 通过、0 失败、0 错误、5 跳过**；冻结旧反例 15/15 另列，不重复叠加。
跳过的 5 项与上一轮同一批：3 条真实符号链接（平台不支持重解析点）、`test_pa03_missing_path_identity_cannot_be_reported_proven`（归档收集器不在公开仓库内）、
`test_u1a_j01_health::test_w_phase_local_boundaries_are_not_wired_into_reuse_yet`（上一轮登记的未接线条目）。

合流过程中的分组证据（每包合流后立即重跑，XML 保留在 `verify/`）。注意：这些是我自己跑的分组计数；
各节「已证明」里出现的更大模块计数（如 262、370、388）是各 lane 自报的定向扫描数，我没有逐个重跑，以它们的报告为准。

```
A 备份族与回归        239 tests / 238 passed / 1 skipped        mergeA_backup.xml  298584419AEDEF19
B 质量与 W2/W3        89 tests / 89 passed                     mergeB_quality.xml ABC8B1C9A731C235
C 读取与覆盖          93 tests / 93 passed                     mergeC_cov.xml     E002C740B419F095
D 事实与覆盖          277 tests / 277 passed                    mergeD_cov.xml     0F6AB2DE615BF4CE
E 审阅                64 tests / 64 passed                      mergeE_org.xml     48B45242B138D10C
F 范围与报告          229 tests / 229 passed                    mergeF_org.xml     1820354029BAD135
集成接线（内容绑定+来源旁车） 160 tests / 160 passed             sidecars.xml       7C97690F754B2F2B
复审后的两处修复      50 tests / 50 passed（旧库读计划）        legacyread.xml     56CB970E0C48D243
                     81 tests / 81 passed（预算上限顺序）      ceiling.xml        10119BC8E3F48D0D
```

## 仍未闭合（本轮不做 / 不可做）

1. **V2 没有作品页的期望内容代次**：`deliveries.source_identity` 是引用身份，`source_pages.content_digest` 是关注列表页摘要。
   交付维度只能 unknown（`expected_content_version_missing`），退出码非 0。要闭合得在采集时写入期望代次——下一轮的数据面工作。
2. **外部 `-wal`/`-shm` 没有所有权模型**：只能保留并命名，没有回收路径（上一轮留下的缺口，本轮未变）。
3. **`organizer/plans.py` 建计划入口**不校验规则指纹（RA-04 的相邻消费点），需要先决定计划命令的规则来源。
4. **`maintain inspect/verify` 仍以 `mode=ro` 打开源库**，会像本轮实测的那样在 WAL 库旁新建 `-wal`/`-shm`（模块 docstring 已写明）；
   `backup` 现在会点名这个差异，但 `inspect` 没有。若要与本轮的承诺一致，下一轮把它接到 `read_plan`。
5. 真实账号写回、图库恢复/释放、云盘往返、真实库长跑：未授权、未做。

## 复审

见同目录 `REVIEW_R3.md`（只读 fork 的逐条裁决与我的处理）。
