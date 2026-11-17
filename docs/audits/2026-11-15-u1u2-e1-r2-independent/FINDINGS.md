# 944ee13 独立复审：逐项发现

审计日期 2026-11-15。基线 `944ee13ea864c47f8ea068292b4d4205d4fa2315`；比较点为上一轮审计 `75f6ad6`。本轮只读产品代码，在独立 worktree 使用临时数据库、合成图片和假传输。没有改真实图库或账号。以下 6 项对应 [13 条可重放反例](evidence/test_adjacent.py)，最终 10 红、3 绿，均无测试设置错误，结果与 XML 摘要在 [results.json](evidence/results.json)。

## RA-01 · P1 · 在线备份仍会删除未认领的 SQLite 旁车

位置：[library_maintenance.py](../../../core/library_maintenance.py#L887)，`_online_snapshot` 的 claim → lease → connect/backup/PRAGMA；写完后才检查剩余旁车。

触发：在公开 `backup_library(..., mode='online_backup')` 的目标 claim 返回后、SQLite 打开前，把临时哨兵的硬链接放在目标 `-journal` 或 `-wal` 名字上。所有路径均在本测试临时目录内，使用真正的 SQLite 写入，没有替换其执行结果。

实测：两个名字均被删除，备份返回成功；同一哨兵的另一个硬链接保留 7,936 字节不变。`-shm` 对照保留并返回 `snapshot_not_self_contained`。因此这次证明的是**删除外来目录项**，不声称已经复现外部哨兵字节被覆盖。若文件没有另一个链接，删除该名字就失去该文件。

主文件 lease 修复有效，但其授权不覆盖 SQLite 自己创建、截断、回收的整个文件族。父目录禁止改名也不禁止目录内出现其他成员。当前 `test_foreign_sidecar_is_kept_and_named` 在 `workspace.record()` 钩子放入旁车，此时 SQLite 已写完，无法覆盖本窗口。

需修：在允许 SQLite 执行有副作用操作前确定完整目标文件族的授权，覆盖 `-journal/-wal/-shm`，并保持到连接关闭；不能仅加一个执行前 `exists()`，也不能事后删除旁车消除报错。无法安全取得能力时明确拒绝，保留外来文件。现有主文件替换、目标 connect/close 失败、取消、父目录降级等测试都必须继续通过。

## RA-02 · P2 · “只读”覆盖查询会创建 SHM，immutable 推断也缺少依据

位置：[follow_facts_v2.py](../../../core/follow_facts_v2.py#L118) `read_only_mode/read_only_connection`；[report_commands.py](../../../core/report_commands.py#L120) `coverage_report_v2`。

触发：合成合法 WAL 数据库，在写者空闲时复制主文件与 WAL，形成有 WAL、没有 SHM 的固定输入；调用真实 `read_only_connection` 并查询。

实测：查询结果正确，但目录从 `copy.db, copy.db-wal` 变为另有 `copy.db-shm`。代码用“有任一旁车”推断索引已存在，逻辑不成立。SQL `query_only` 不等于文件系统无写入。SQLite 官方也明确说明，readonly WAL 的可用条件可以是旁车已经存在，或者允许创建它们。[SQLite WAL 只读说明](https://www.sqlite.org/wal.html#read_only_databases)

同一选择器把“两者均不存在”当作数据库无人使用、可以 immutable 的证明；这也不足以排除 rollback-journal 写者或随后出现的写者。`immutable=1` 会跳过锁和变更检测，不能仅靠调用前后 `(size, mtime)` 替代一致快照。[SQLite URI 参数](https://www.sqlite.org/uri.html#uri_query_string_parameters)

本项的已复现故障是新增 SHM；未声称已复现数据库损坏或混合版本结果。还应纠正关闭报告“读取期间库被改写一定报 database_changed_during_read”的概括：代码只对 immutable 模式比较主文件属性，正常锁定读应允许并发提交。

需修：明确 SQL 只读与源文件族不变两种契约；只给受控不可变快照使用 immutable。活跃库通过正确的锁/一致快照协议读取，不能以“没有旁车”选无锁。若严格要求源目录无任何副作用而无法保证，则拒绝并解释，不能返回假只读证明。

## RA-03 · P2 · V2 覆盖统计没有沿真实集合和页面链接取数

位置：[follow_facts_v2.py](../../../core/follow_facts_v2.py#L190)，`coverage_facts` 只遍历所有 batch_items；delivery/artifact 按当前 job_id 取，未使用 `batch_item_pages`。

三个公开仓储路径的反例：

| 场景 | 仓储事实 | 新摘要结果 |
| --- | --- | --- |
| 完整关注快照含 2 位画师，尚无作品行 | follow_artists 有 2 人 | artists_total=0、artists=[] |
| 1 部作品暂停后继续并认领原待下载页 | 仍是同一作品、两个批次 | works_seen 从 1 变 2；expected 重复 |
| 第二任务复用第一任务已落盘文件 | 本批 existing=1、completed=1；artifact-only 链接 | observed=[] |

这并非“期望内容代次未知”造成。即使交付维度暂时必须 unknown，画师总体、唯一作品数和已有页仍应正确。页面引擎后续可能去重 expected，但不能修复 adapter 已重复累计的 works_seen，也不能凭空补回被漏掉的链接。

需修：先冻结本次任务的画师集合、跨批次作品身份与页面链接规则；从该任务对应的 snapshot/progress 找到无作品行的画师；以唯一作品身份累计，明确当前/历史批次的取舍；按 `batch_item_pages` 解析 delivery 或 artifact-only 关联，允许引用其他 job 的合法历史成果。不把“最新 generation 最大”当作所选任务实际采用的版本。

## RA-04 · P2 · 看图证据没有绑定已保存的审阅与消费时版本

位置：[rules.py](../../../organizer/rules.py#L234) `review_evidence`、[classify](../../../organizer/rules.py#L415)。

第一条反例使用真实临时 `organizer.store.Store`：仅存一份 sample，review 表完全为空；根据该 sample 组成 receipt 字典，调用公开 `review_evidence()`，结果却是 sufficient（12/12）。函数读取 sample，但不读取/核对 receipt_id 对应的已保存 review；复制样本索引、哈希与 attestation 字符串就能越过“确实审阅过”的门槛。这不是直接手工构造内部 VisualEvidence 对象的攻击。

第二条：按 v1 验证得到 sufficient 的证据对象交给 v2 `RuleSet` 的 `classify`，结果仍为 high、needs_more=False。消费者只看 sufficient，不校验其 rules_version 与当前规则。`classify` 也允许从 evidence.visual_evidence 携带该对象，因此 `changed_by_version` 的旧/新规则比较同样需要消费时门禁。

纯标签无 visual 的对照维持 low、needs_more=True，原 EA-07 修复有效；本项针对新增证据通道。模块尚未接到真实分组写回，所以不能把此问题描述为已经错误修改网站账号。

需修：以 receipt_id 读取 canonical review；核对传入副本、sample 与审阅记录，而非用自由字典作为审阅存在证明。证据携带 account/artist/sample/snapshot/规则身份，在实际分类/计划消费者校验。规则内容指纹与版本标签如何配合需要先冻结；未知或过期不得 high。保留不同作品计数、多页折叠、缓存 SHA 和既有真实 review 门槛。

## RA-05 · P2 · 分组范围丢失账号身份，报告账号门禁被绕过

位置：[group_scope.py](../../../organizer/group_scope.py#L512) `build_scope` 构造 source；[report.py](../../../organizer/report.py#L201) `_inputs_agree`。

触发：账号 B 的完整快照经真实 build_scope 构造成范围，与账号 A 的批次配合，成员集合及 snapshot 标签相同，以单独检验账号门禁。实测 source 中没有 account_id，build_report 不报错。

根因：生产者丢掉 snapshot.account_id；消费者只在 scope.source 已经带非空账号时比较。报告不会把账号来源缺失加入 unknowns。批次规划还允许没有 snapshot_id，所以不能用快照字符串的偶然不同替代账号绑定。

需修：账号身份沿 snapshot → GroupScope → 序列化/反序列化 → batch-report 完整传递，参与适当的指纹/版本策略。已知不同直接拒绝；历史输入缺身份明确 unknown/非成功，不补成当前账号。`--account` 只能核对调用上下文，不能证明 scope 来源。报告现有 website_write_back=not_proven_here 的限定应保留。

## RA-06 · P2 · 解码成功没有绑定到被认证的内容字节

位置：[quality_check.py](../../../core/quality_check.py#L472) `check_file`，版本前哈希、结构/解码、版本后哈希分开按路径读取；[library_maintenance.py](../../../core/library_maintenance.py#L2891) `_decode_outcome`。

反例构造 A/B 两份同尺寸 1×1 indexed PNG：A 的像素索引超出调色板，真实 strict 检查 decode=failed；B 为合法 PNG。调度钩子只在真实解码器执行期间把路径换成 B，随后恢复 A 和原 mtime；并未伪造 decoder 返回值。

结果：磁盘最后确实是 A，但报告 decode=ok、path_stability=unchanged，`_decode_outcome` 返回 0。前后 A 的哈希一致，只能说明两次哈希观察相同，无法证明中间解码的是 A。普通固定文件路径正常，本项限于观察期间发生受控内容变动的 ABA 窗口。

需修：让结构/解码证据绑定其实际读取的内容摘要或受控稳定快照；内容身份不一致/无法证明时非成功。仅增加第三次路径 stat/hash 或禁止 rename 的句柄不够，后者不能排除对同一文件原地改写。预算仍按真实读流计费，快照空间与取消也必须受限。

## 证据口径与整理问题（P3 / 不另增产品编号）

- 作者 fullI.xml 的 `tests=3424` 是总数，含 5 skipped，所以通过数为 3419。加 Tk 118、presentation 9 后应为 **3546 通过、5 跳过、总计 3551**，不是 3551 通过再加 5。XML SHA 与作者记录一致。本轮设置已入库 collector 路径后，其中一条不再跳过，独立复测为 **3547 通过、4 跳过**。详细逐项计算见 results.json。
- 本轮仍跳过 3 个真实符号链接用例及 1 个历史 W 阶段登记。后者是手工登记，不能把其长说明视为本轮每个边界仍缺失的重新判定。真实外置卷、迁移/恢复、真实账号写回和云盘往返没有验收。
- S0 的 V2 表/字段说明必须与真实 DDL 对齐；不要把 source_pages 的列表摘要或 source_identity 的 URL/策略身份写成图像内容 SHA。V2 缺 expected 内容代次而保留 unknown 是正确限制，不计为新发现。
- 本轮盘点 66 个活跃 worktree、73 个归档、7 个含未提交材料。比上一轮新增 8 个 worktree；没有缺失旧路径。新增脏树是 w1_baseline 的未跟踪测试、w4_organizer 的 `_w4_tmp/`（21,417 文件、672,143,484 字节）。本轮只做摘要保全与导航，不删除。历史 5 个脏树继续原位保留。
- 现有红基线 15/15、更早 17/17 均通过；applied→planned 回退被正确拒绝，未误报成新的写回重发问题；外来 SHM 保留、纯标签不冒充审阅也均为绿色对照。
