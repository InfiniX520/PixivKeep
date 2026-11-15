# U1/U2 修复、接线与 E1 离线实现：独立发现

固定产品 `5661d8bb7e4f39b2ee4c5ea09257bd5cc4cd7651`，日常树 `4ba891e`。比较起点是实现分支祖先 `031627c`，不是上一轮审计文档提交 `a89842b`。本轮只写审计材料，不改产品。复现全部使用本批临时目录、合成库/图片/快照，无真实账号和图库。

## 结论和分级

**1 P1 + 8 P2**。这不是“旧修复全无效”：从 `a89842b` 原样提取的 17 条反例已经 17/17 通过。新的 15 条独立检查是 **14 条失败、1 条正常往返通过**；所有失败均到达业务断言，没有以导入错误冒充漏洞。两组结果同时保留。

P1 指可能越权破坏本地文件的写入路径；P2 指错误的完成/范围/预算判断，或当前入口无法消费本项目实际状态。E1 新模块的问题目前主要发生在离线 API/命令，不能据此声称已改错真实网站账号。

| 编号 | 级别 | 缺口 | 已复现入口 |
| --- | --- | --- | --- |
| EA-01 | P1 | SQLite 在线快照先写后验所有权；父目录身份未约束 claim | 公共 `backup_library` 与 `TemporaryWorkspace.claim` |
| EA-02 | P2 | 命令字节预算漏计解码；哈希阶段不响应分块取消 | `check_file` + 真实读流计数 |
| EA-03 | P2 | 页面已交付不比对期望内容代次，同状态异内容不冲突 | `build_coverage` |
| EA-04 | P2 | 新桌面覆盖率读 legacy 库，与 V2 实际写入断开 | `DesktopRuntime._coverage_summary` |
| EA-05 | P2 | 分批预算裁剪后跳过未选画师；applied 未核验无法续跑 | `plan_batches`、`resume` |
| EA-06 | P2 | Batch 嵌套别名、GroupScope 可赋值破坏不可变范围 | 普通 `as_dict` / 属性赋值 |
| EA-07 | P2 | 标签分数代替多图审阅证据，零作品也高置信度 | `rules.classify` |
| EA-08 | P2 | 范围完整标志掩盖成员事实矛盾 | `group_scope.build_scope` |
| EA-09 | P2 | 报告明确不覆盖批次，却仍退出 0 | 真正 `organizer.cli.main` |

代码行号以下列冻结版本为准；后续修复可变动行号，函数与原始反例是稳定定位入口。执行步骤见 [DEVELOPMENT_GUIDE](DEVELOPMENT_GUIDE.md)，逐次运行记录见 [results.json](evidence/results.json)。

## EA-01：SQLite 在线备份的写入授权仍在写入之后

定位：`core/library_maintenance.py:813–857` 的 `_online_snapshot`；`core/workspace_ownership.py:125` 的 `verify_owned`、`:343–397` 的 `claim`。

`workspace.claim(target.name, purpose="online_snapshot")` 创建目标但返回的能力对象被丢弃。随后 `sqlite3.connect(str(target))` 按路径重新打开、`origin.backup(destination)` 写入、执行 journal PRAGMA。直到关闭连接才 `verify_member`。该检查能够拒绝“成功备份”，却不能撤销已对错误文件的写入。

独立测试通过公共 `backup_library(..., mode="online_backup")` 调用，在 claim 返回前将本批空目标替换成指向本批哨兵 SQLite 的硬链接。**确实返回 `snapshot_target_replaced`，但哨兵从 8,192 字节变成 331,776 字节，原数据被覆盖。** 替换、源库、哨兵全在 pytest 临时目录内。这是确定性调度文件替换窗口，不需要真实并发、管理员权限或符号链接。

第二条反例：adopt 空工作目录后，重命名原目录、在原路径新建另一个目录，接着 claim。它会在替身目录里创建 `unexpected.bin`。`parent_identity` 被记录，但没有证明父目录仍是最初 adopt 的目录。此处单独证明父目录约束缺失，不夸大成已经覆盖任意目录文件。

实际影响：在线备份是健康数据库自动模式的常用路径。正常单进程、没有替换时本测试不证明会损坏；但并发维护/路径替换/其他本地进程进入该窗口时，当前“报错”不构成保护。上轮普通拷贝路径通过 fstat 后再截断已改善，不代表 SQLite 路径也获得同样保证。

修复要求：写入前获得并保持目标与父目录能力；SQLite 按名打开期间不能释放能够阻止替换的保护。明确 Windows 共享/重命名语义，不把“先 stat 再 connect”当作原子授权。允许保护机制使替换本身失败；不能为让攻击钩子成功而主动关闭保护句柄。修复后同时验哨兵字节不变、正常在线备份可核验、并发源库写入仍能得到一致快照，以及失败后只清理自有成员。

## EA-02：预算只有阶段标签，没有覆盖全部实际 I/O

定位：`core/quality_check.py:358–378`、`:392–471`；`core/decode_limits.py:166–260`。

`_receipt_bytes` 从 `DecodeReceipt` 读取不存在的 `bytes_read`，默认永远 0。解码器自己的读量没有回传到命令总账；两次 `_sha256` 读到 EOF 后才 charge，`charge` 的拒绝结果没有控制后续判定。`CommandBudget.enter` 仅检查取消/时间，无法限制某阶段即将读取的字节。哈希循环内没有取消或时间检查。

有效 67 字节 PNG，strict 模式、上限 202 字节：计数包装器观察到 **268 字节**（前哈希、结构、解码、后哈希），报告只有 **201 字节**，最终 `_decode_outcome` 为 **0**。另一条 3 MiB 合成文件在第一次 1 MiB 读取后设置取消，哈希仍继续把余下 2 MiB 读完。

影响：报告的 command 预算和用户停止操作不能限制实际读盘；大文件上的资源占用可能远超声明。不是 PNG 像素判定错误，也不是本轮已经触发超大内存分配；本证据限于 I/O 计费和取消时机。

修复要求：各阶段共用实际读流预算，读取前限定尺寸，读取后计实际字节；每个有界块检查取消和时间。decoder 返回真实消耗，终态不能靠后续 stage 清空。预算耗尽后不能通过第二次完整哈希来“补证明”；未能完成稳定观察须非成功。另行明确 command 是单文件调用还是整个多文件 CLI，不能让每文件新建预算却报告整条命令总额。

## EA-03：非空内容版本不等于匹配内容版本

定位：`core/follow_coverage_counts.py:158–237` 的 `_page_identity`、`_page_evidence`、`_delivered`。

expected 被化简成 page identity 集合，丢掉期望内容版本；observed 将所有非空版本放进 set；`_delivered` 只检查状态和 set 非空。同页两条观察只有状态不同才冲突，内容不同而状态相同会并存。

合成完整发现事实：期望作品 100 第 0 页的 SHA 为 B；唯一 `already_verified` 观察为 A，输出仍 `delivery=complete, overall=complete`。再加一条相同状态、版本 B 的观察，也仍 complete。两条反例分别覆盖“仅过期”和“新旧矛盾”。

影响：当真实页事实接入后，旧内容收据可能证明新内容完成。当前 legacy crawl reader 未提供页面事实，常返回 unknown；这限制了当下 UI 的影响，但不消除纯引擎错误，不能因尚未接线就声明契约闭合。

修复要求：expected 保留 page→内容代次映射；观察必须与期望代次、质量策略等实际契约匹配。重复的同版本观察可去重，异版本不能靠集合中“有一个匹配”默认成功；若有明确顺序/权威观察规则，必须随证据持久化并测试，不能用列表顺序猜最新。

## EA-04：桌面覆盖率连到了另一套数据库

定位：`core/collection_desktop.py:139–150`；`core/crawl_state_reader.py:30`；`core/report_commands.py`。

`DesktopRuntime._crawl_state_path()` 固定返回 `download_logs/follow_crawl_state.sqlite3`；reader 要求 `crawl_runs`、`artist_progress`。当前桌面下载器通过 `LibraryRepository` 使用 `library.sqlite3`、V2 的 follow snapshot/artist/job/batch 事实。旧库写入出现在 legacy fetcher/runtime 路径，新的覆盖查询没有 V2 适配器。

反例在合成 `library.sqlite3` 中通过真实 repository API 创建关注任务和快照，确认有一位画师；调用实际桌面 `_coverage_summary(job_id)`，得到 `database_missing`，查询目标正是不存在的 legacy 文件。即使旧库碰巧存在，也不能因此证明它描述当前 V2 批次。没有把 Legacy CLI 对显式旧库的合法支持判为错误。

影响：新用户/只有 V2 数据的用户点覆盖摘要无法获取当前结果；混有旧数据库的用户还存在来源误认风险。UI 单元测试用 fake runtime、report 测试创建旧表，都不会发现此断开。

修复要求：先冻结当前任务、job、batch、snapshot 的选择语义，再做 V2 只读事实适配器。UI 从所选任务传明确 ID，不让用户猜一个旧 run_id。旧 CLI 如需保留应标明 legacy provider；不要复制旧库或让界面临时创建其表来“修复”报错。用真实 V2 合成仓储→runtime→controller/panel 走完整链验收。

## EA-05：预算裁剪与批次游标不一致；写回后核验被跳过

定位：`organizer/batches.py:42–45`、`:367–404`、`:427`、`:460`、`:477`。

`plan_batches` 按 max_artists 对 pool 切块，`plan_batch` 再按更紧的预算截断块，但下个块的 offset 仍增加 max_artists。5 位画师、每批最多 3 位、请求预算仅够 1 位时，结果只包含 **1、4**，2、3、5 无批次也无显式剩余集合，违背函数声明的集合守恒。

`applied` 又在 FINAL_STATES 中而不在 OPEN_STATES 中。按 sample→review→plan→apply 推进一位画师，尚未 verify 就 `resume(batch,{}) == ()`。状态转换表本身允许 verify，但续跑选取不会选它。附带静态观察：`rebuild` 会拒绝从 applied 更新为 verified；正常 active 批次全部最终态时，status 的更新还与 paused 分支耦合。后两条需修复时加独立状态表用例，不计入本轮已复现条数。

影响目前是离线调度 API；既有真实 apply/reconcile 没有在本轮被替换，不能称真实网站核验已被取消。

修复要求：pool = 已完成 + 已规划 + 显式剩余，集合互斥且不漏；预算零容量返回可续状态，不空循环、不吞名单。区分“写回请求已执行”和“读回验证完成”，resume/rebuild/advance 共享状态真值表。

## EA-06：不可变数据结构只冻了一层

定位：`organizer/batches.py:159–217`；`organizer/group_scope.py:160–194`。

Batch 是 frozen dataclass，但 scope/policy 仅 `MappingProxyType(dict(...))`；内部 list/dict 仍共享。对 `dump=batch.as_dict()` 执行 `dump['scope']['artist_ids'].append('999')`，原 batch.artist_ids 同步变成 1、999，fingerprint 改变，steps 仍只有 1。

GroupScope 是普通可赋值类。`scope.artist_ids=('999',)` 可以成功，原 fingerprint 完全不变。这不涉及 object.__setattr__ 等反射绕过，而是普通公共操作。

影响：调用方加工导出内容、恢复检查点或复用对象时，冻结范围可能悄悄改变，与已有 fingerprint / steps 不一致。读取时重算 hash 不能修复已在内存发生的共享修改。

修复要求：构造时规范化并深度复制/冻结所有嵌套集合；导出必须完全脱离原对象。GroupScope 禁止普通赋值；反序列化核对成员、blocks、steps、来源和版本的一致性。fingerprint 是一致性摘要，不是签名，文档不能宣称可防恶意重算后的篡改。

## EA-07：标签规则的权重被当成实际看图证据

定位：`organizer/rules.py:162–225`；对照 `docs/optimization/2026-11-15/E1_PLAN.md` §3.2、L2 验收要求。

输入只有 `tags=['cat'], viewed_works=[]`，一条 weight=2 的规则即可得到 `confidence='high', needs_more=False`。分类器只处理标签匹配、累计权重，未读取实际看过的作品数量/身份/证据。

用户要求每位查看多张作品；新契约也明确证据不足不进计划。现有 `organizer/samples.py:188–218` 的 review 仍要求实际查看声明、样本 SHA 和至少 min(12, available_works) 个不同作品；**旧门禁仍在，本发现是新增模块未承接门禁，不是旧门禁被删。** 新 rules 模块目前尚未接到真实计划写回链。

修复要求：规则匹配分数和视觉证据充分度分开。对已验证审阅收据进行绑定与计数，重复页不能充当不同作品；少作品画师只能依据有证据的 available_works 降低门槛。标签只能建议候选分组，不能伪装成模型已看图。

## EA-08：分组成员事实矛盾却标 complete

定位：`organizer/group_scope.py:148–157`、`:271–326`。

快照有两份成员事实：每位画师的 groups 和 memberships 的每组 ids。build_scope 只从 artists 推导成员，memberships 用于识别组名，完整性仅信全局 status/memberships_complete。

合成输入：memberships.show.g.ids = [1,2]，artists 只有 1，顶层 status=complete、memberships_complete=True。结果范围只有 1，却标 **complete**。真实快照采集器 `collect_memberships` 有交叉校验，正常成功产物应一致；本轮明确测试的是新离线导入边界对损坏、拼错版本或不一致文件的处理，不声称正常采集器必然产出这种输入。

修复要求：在纯范围入口验证所用分区完成状态、成员集合与 artist 行、可见性、catalog 的一致性；不一致应拒绝/partial/unknown 并给原因。不能只复述顶层布尔值。续跑时成员相同但完整性降级也须保留该事实，不能把旧 complete 凭据复用为新的 complete。

## EA-09：批次报告的退出码没有消费自己的范围判断

定位：`organizer/report.py:173–275`；`organizer/offline_cli.py:207–242`。

report 会计算 `covers_batch`，但没有把 False 纳入 unknowns/拒绝。CLI 只检查 scope.completeness 与 unknowns。通过真正 CLI 输入三份合法 JSON：batch 包含画师 1；complete scope 包含画师 2；decision 声称 1 unchanged/keep。输出明确写 `covers_batch:false`，仍返回 **0**。

影响：自动接续或人工看到成功退出码，会以为这批报告的输入已对齐。不是报告证明了真实写回成功——其 proof_limits 已明确不证明网站写回；问题在于报告自身范围都不成立仍称输入可用。

修复要求：在组合前校验 batch/scope/decisions 的身份、成员包含关系、snapshot/rules/account 来源；范围不匹配不能成功。若允许更大 scope 覆盖子批次，应显式定义包含关系，不强求错误的全等。补充导入坏形状、互相矛盾决策字段的类型化拒绝；不要只在 JSON 最后多打印一行警告。

## 整理和证据方面的结论

- 本轮相对 `031627c` 有 67 文件、9,998 行新增、474 行删除；不能再仅凭“上一轮反例全绿”覆盖新增 E1 的正确性。
- 新候选从旧产品分支继续，没有包含 `a89842b` 的审计文档提交。对比 a898 会看似“删除报告”，实际上是祖先关系不同。本轮将上一轮 5 个报告/证据文件按 Git blob 原样补回，原日期和原结论保持。
- docs/README、IMPLEMENTATION 顶部与 TASK_PLAN.active_workstream 仍指向 11-14 R2，和末尾新增交付日志不一致；本轮只更新当前入口，旧日志逐条保留。
- 工作树从 43 活跃/73 归档变为 58 活跃/73 归档；其中本审计自身占 1 个新增活跃树。5 个既有脏树保留，不对未知未提交材料做自动合并或删除。
- Tk 在本轮指定解释器、`-s` 和显式 Tcl/Tk 路径下的结果应按实际 XML 阅读；它只能证明该组织方式下通过，不能据此普遍断言所有 Tcl 初始化错误已根治。
- 真实迁移、外置卷、图库/云盘往返、真实整理写回、多格式第三方严格解码仍未因此轮审计得到生产证明。
