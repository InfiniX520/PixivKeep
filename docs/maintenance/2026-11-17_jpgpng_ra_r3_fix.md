# 2026-11-17 944ee13 复审六项修复记录（RA-01…RA-06）

审计提交 `5d1e676`（被审候选 `944ee13`）结论是 1 P1 + 5 P2，并明确"修复有效但不能宣称九项全部闭合"。
本轮从审计分支开 `fix/a1-ra-20261117`，按 `docs/optimization/2026-11-17/S0_CONTRACTS_R3.md` 的六条契约关闭这六项。
日常树 `E:\Zero9\JuiLiu\JpgPng` 全程停在 `4ba891e`，未切换、未部署。

## 做法

1. **先冻结契约再 fork**：六条契约写清生产者、消费者、未知、矛盾、过期五种情形；C 包的读取选择表**先由探针实测**再定稿，
   两处未经实测的推断被实测推翻后按实测改写（§10.2）。RA-04 两条反例互斥也在开工当天裁决并写进 §10.1。
2. **五路并发、文件不相交**：A 文件族授权、B 质量内容绑定、C 读模式、E canonical review、F 账号来源；D（V2 关系取数）与 C 共用
   `core/follow_facts_v2.py`，按契约在 C 合流后才开工。主智能体只写契约、CLI 接线、`_decode_outcome`、文档与合流。
3. **每个包先红后绿**：13 条相邻反例在 `5d1e676` 上是 **10 红 / 3 绿**（`verify/ra_before.xml`），
   每包在自己分支上复跑并留下 before/after XML。B 的 12 条新用例还额外在未修代码上跑出 **12 红**。
4. **冻结边界逐字节不动**：`docs/audits/**` 与 `tests/test_boundaries.py` 在本分支 `git diff 5d1e676..HEAD` 为空。

## 逐项结果

| 编号 | 关键改动 | 红→绿 |
| --- | --- | --- |
| RA-01 P1 | 产物在私有 `.staging-<id>` 里由真实 SQLite 生成；成品目录一次 `os.rename` 发布；连接前预检三个旁车、关闭后复核、发布前复核外来名；`icacls` 收紧 ACL（失败记 degraded）；禁止 OFF/MEMORY 等削弱 | ra01 2 failed/1 passed → 3 passed |
| RA-02 P2 | `read_plan()` 按实测选择读取方式并返回证据；WAL 缺旁车改走私有副本（前后比较名字集合与 (size,mtime_ns)）；`immutable` 只能由调用方显式声明；报告与文本写明承诺 | ra02 1 failed → 1 passed |
| RA-03 P2 | facts 沿 job→批次→`events(follow_snapshot_selected)`→快照成员→`batch_items`→`batch_item_pages`→`deliveries/artifacts` 取数；画师总体三分支且未知不写 0；同 job 按 illust_id 去重；允许跨 job artifact 复用 | ra03 3 failed → 3 passed |
| RA-04 P2 | `review_evidence` 读已落库 review 并逐身份字段比对；`VisualEvidence` 带 `review_id`/`rules_fingerprint`；`classify`/`changed_by_version` 消费时核对规则指纹 | ra04 2 failed → 1 passed（#2）+ #1 停在第一个断言（见关闭报告 §10.1） |
| RA-05 P2 | `GroupScope.account_id` 与 `source.account_id*/identity_fingerprint`；报告先判身份再判包含；账号未知 → unknown 且退出 5；`ensure_current` 五态 | ra05 1 failed → 1 passed |
| RA-06 P2 | 解码器改吃内容通道（边读边哈希计数，读完补读剩余字节）；报告新增 `content`/`content_bound`；四类观察互相矛盾即降级；CLI 文本加内容行，`_decode_outcome` 显式要求绑定 | ra06 1 failed → 1 passed |

## 用户可见的变化

1. `maintain backup`：manifest 新增 `family`（precheck/foreign/acl/publish/published/root）与 `source_sidecars_created`；
   成功时文本多一行"注意: 本次读取让 SQLite 在源库旁新建了 …（无库内容，未删除）"（仅当确实新建时）。
   新的错误码：`backup_target_family_occupied`、`backup_target_family_foreign`、`backup_publish_failed`、`backup_staging_failed`。
2. `maintain coverage`：JSON 新增 `read` 与 `facts_notes.read_mode/read_promise/read_note/artist_cohort/snapshot_*/page_links`；
   文本多一行"读取方式: …"，并在画师总体未知时明确说未知（不打印 0）。
3. `maintain decode-check`：JSON 每个文件新增 `content` 与 `content_bound`；文本多一行"内容: 已绑定/未绑定"；
   未绑定的解码不再算通过（退出码 5）。
4. `organizer` 离线命令：`scope-plan` 在快照没有 account_id 时退出 **5** 并打印"账号: 未记录"；
   `batch-report` 在范围账号未知时退出 **5** 并多一行"范围账号: 未记录（无法核对账号来源）"；
   `report_version` **2 → 3**，新增 `scope.account_id`、`proof_limits.account/complete/incomplete_reasons`、
   `provenance.scope_account_id`。`--account` 的帮助改成"只核对调用上下文"。
5. `organizer` 审阅：新 review **可以**带 `rules_fingerprint`（带 `RuleSet` 时自动算）；但**不带指纹的 review——无论新旧——消费时一律 unknown**
   （`visual_rules_unbound`），不会被当成"看过这些图"。写入端目前照存这类记录（复审指出这一点），拒绝它需要先决定审阅命令的规则来源，
   记在"未做与遗留"。

## 验收（命令与结果）

6. `maintain coverage`（旧 `--run` 形态）：干净关闭的 WAL 状态库改走私有副本，文本不再说"只读，legacy"，
   改为按实测的计划说明读取方式（`facts_notes.read_mode/read_promise`）。
7. `maintain decode-check`：预算顺序修正后，解码器已读满上限时不再补读剩余字节——那次运行的内容维度会显示"未绑定/不完整"，
   而不是"已读整文件"（旧顺序实测在 300 字节上限下读到 400 字节仍标 observed）。

解释器 `D:\Miniconda3\envs\JuiLiu\python.exe`，`PYTHONNOUSERSITE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，
参数 `-B -X utf8 -m pytest -p no:cacheprovider`，每次新 basetemp；一键脚本 `.runtime/jpgpng_ra_fix_20261117_01/verify/run_round.py`。

## 验收矩阵（代码与测试的最后一次提交是 `2aa18c3`，矩阵在其后运行）

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

合流过程中的分组证据（每包合流后立即重跑，XML 保留在 `verify/`）：

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

## 未做与遗留

1. 生产验收全部未做：真实库、真实账号写回、外置卷、云盘往返、真实备份恢复。
2. V2 没有保存作品页的期望内容代次，交付维度只能 unknown（退出码非 0）。要闭合得在采集时写入期望代次——数据面工作。
3. 外部 `-wal`/`-shm` 没有所有权模型：只能保留并命名，没有回收路径。
4. `backup` 会指名它让 SQLite 在源库旁新建的 `-wal`/`-shm`，但**不删除**；`maintain inspect/verify` 仍直接以 `mode=ro` 打开源库，也会留下同类文件（模块 docstring 已写明）——下一轮若要与本轮承诺一致，把它接到 `read_plan`。
5. `organizer/plans.py` 的建计划入口仍不校验 review 的规则指纹；`organizer/samples.review` 仍会存下未带指纹、永远不能用的审阅记录。
   两者都需要先决定计划/审阅命令的规则来源，属于下一轮的产品决定。
6. 副本一致性用 (size, mtime_ns) 前后比较，不做源侧第二次全读；同尺寸改写并精确恢复 mtime 的 ABA 不在防护范围。
7. 备份的预检→connect 之间有窗口：同用户进程若监视私有 staging 目录并注入文件，我们只能由发布闸门事后拒绝发布。
