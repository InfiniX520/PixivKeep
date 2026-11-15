# 031627c 独立审计：发现与证据

日期：2026-11-15。版本、执行口径及统计见 [README](README.md)；后续实施见 [DEVELOPMENT_GUIDE](DEVELOPMENT_GUIDE.md)。以下行号固定于被审产品 `031627c64eb2aac5605b05a21a47a7c0b3c5fef2`。严重度按实际影响和现阶段接线范围判定，不按测试失败数量累计。

本轮确认 **1 P1 + 7 P2**。17 项独立用例为 **15 红 / 2 绿**，一个问题可以有多个反例。没有修改产品实现来让测试变绿。证据用合成 SQLite、67/82 字节 PNG 与临时目录构造；未接触真实库、图库、账号或上传资料。

## UX-01 · P1 · 临时文件所有权没有贯穿写入与 sidecar 清理

**位置**：`core/workspace_ownership.py:146`（claim）、`:178`（record）；`core/library_maintenance.py:483`、`:513`、`:1526`、`:1595`。

R2 已移除递归清理并拒绝陌生普通文件、junction 等，这是实质修复。但 `claim()` 用 O_EXCL 创建后立即关闭句柄，只返回路径；`_copy_and_hash()` 随后按此路径重新 `open('r+b')` 并 `truncate(0)`，没有先把写句柄身份与 claim 对比。复制结束再次 `record()` 会接纳当前对象，不能补救已发生的写入。

独立用例在 claim→复制的边界，把已创建的空文件替换成指向本批合成 sentinel 的硬链接，再调用原复制函数。`verify_backup()` **返回 ok=True**，但另一文件由 **20 字节变成 331,776 字节**，内容被 SQLite 副本覆盖。这不是仅“回执不准确”，而是核验命令发生了越过本次所有权的内容修改。硬链接创建及所有变更都限定在本批 pytest 临时目录。

同一所有权缺口还出现在 `_record_sidecars()`：只因文件名恰好是数据库名加 `-wal/-shm` 就调用 `record()`。在 `inspect_library()` 返回后放入本批另行创建的 `library.sqlite3-shm`，核验仍报告成功，且该陌生文件被清理。精确名称也不是创建来源的证明。其注释“没有根据后缀推断”不符合实际行为。

**反例**：`test_ux01_claimed_destination_replacement_cannot_overwrite_other_file`、`test_ux01_unowned_sidecar_must_not_be_adopted_and_deleted`。

**修复要求**：所有权至少覆盖创建→写入→SQLite 使用→清理；不要用第二次 record 掩盖身份替换。持有排他创建得到的句柄并据此写入；打开其他句柄时，在任何 truncate/write 前核对目标、父目录、链接及身份。无法证明由本次 SQLite 创建的 sidecar 留存并报告，不能自动收养。需要明确 Windows 文件共享/替换语义，不要仅再加一个存在性检查。

**边界**：反例是确定性竞态注入，证明该执行窗口真实存在；不表示用户已遭受数据损坏或远程可直接利用。`_copy_file()` 及在线备份的目标生命周期也应沿同一原语检查，不能只修复本次 `_copy_and_hash()`。

## WR-04 剩余 · P2 · sidecar 身份读取异常仍逃出维护 CLI 协议

**位置**：`core/library_maintenance.py:513`、`verify_backup()` 约 `:1900/:1931`；`core/workspace_ownership.py:178`。

`_safe_cleanup()` 能包装 discard 失败，但 finally 中先调用 `_record_sidecars()`。其 lstat 失败产生 `WorkspaceError(RuntimeError)`，仍可在清理前逃出；finally 再次调用还可能覆盖原失败。

在已成功建立的合成备份上，只对核验临时目录 `-shm` 的 lstat 注入 `PermissionError`，通过真实 `main(['--json','verify',...])` 复现裸 `WorkspaceError`，没有约定的错误 JSON/退出码 3。不是把整个业务函数替换成一个人为异常。

**反例**：`test_wr04_sidecar_identity_failure_stays_in_typed_cli_protocol`。

**修复要求**：将工作区建立、使用、登记及清理全部纳入统一结果协议；主失败与清理失败分字段保留，finally 不得替换主失败。身份未知即留存。不要把任意程序错误吞成成功，也不要只在 CLI 最外层捕获后丢掉工作区回执。

## UX-02 · P2 · 索引色 PNG 非法像素被严格解码判定为通过

**位置**：`core/png_decode.py:151`、`:305`；入口 `maintain.py decode-check`。

构造一张合法签名、IHDR、CRC、zlib、行长度的 1×1 索引色 PNG，PLTE 只有一个色项，但像素值为 1。严格检查返回退出码 **0**、`structure=ok`、`decode=ok`、`pages=all_frames`。相同构造的像素值 0 正常对照通过。

W3C PNG 的 PLTE 规则将超出调色板范围的像素标为错误；规范同时规定解码器可采取显示恢复方式，不能把“可恢复显示”解释成“严格检查没有错误”。本实现只反滤波字节，没有检查解出的索引是否落在实际调色板范围内。[W3C PNG 第三版，11.2.2 与 13.1](https://www.w3.org/TR/png-3/)

**反例**：`test_ux02_palette_index_beyond_palette_is_not_a_strict_pass`；正对照 `test_control_valid_palette_index_passes`。构造不使用产品 PNG 生成助手，避免生成器和验证器共享同一错误。

**修复要求**：按色彩类型、位深及每行实际像素检查样本；低位深需区分像素和行末填充位；Adam7 要用每趟尺寸。恢复容错若保留，必须单列 receipt，严格验收不能无提示成功。先建立规范驱动的有效/无效矩阵，再决定继续维护自研解码器还是引入一个经过核验的成熟可选组件。

**范围**：当前严格真解码仅支持 PNG；JPEG/GIF/WebP 明确能力不可用是正确行为，不列为本轮缺陷。尚未把该 CLI 接入原图删除或云端释放门。

## UX-03 · P2 · 质量维度与最终退出码没有共同证据门

**位置**：`core/quality_check.py:154`、`:176`、`:224`；`core/library_maintenance.py:2639`。

1. `check_file()` 前后哈希不同，只写 `unchanged=False`；`_decode_outcome()` 只看 structure/decode。把检查完的有效 PNG 换成另一张有效 PNG，公开 CLI **退出 0，unchanged=False**。结论描述的是旧字节，不能作为当前路径通过的回执。
2. `quality_evidence()` 仅凭 `expected_size is not None` 就写 `transport=length_matched`。给 67 字节图片传 expected_size=68，结构报告 `image_size_mismatch`，传输维度却仍说长度匹配。

**反例**：`test_ux03_changed_final_file_cannot_receive_success`、`test_ux03_mismatched_expected_length_is_not_length_matched`。

**修复要求**：把质量观察绑定到同一个内容版本，组合层把变化/未知稳定性当作不能给完整成功结论的条件；长度匹配必须来自实际比较。保留“检查了什么”与“当前路径是否仍是它”的区别；本次哈希不能伪造历史或来源证明。不要把前后哈希相同宣称为对任意 ABA 替换的完整证明。

## UX-04 · P2 · 解码预算与停止信号没有覆盖前后全文件工作

**位置**：`core/quality_check.py:154`、`:213`、`:224`；`core/decode_limits.py` 与调用方。

`check_file()` 先做不看 stop/limits 的完整 SHA-256，再做结构检查，然后才进入有解码预算的路径，最后再次完整 SHA-256。结构检查本身也会读流、处理 PNG；解码阶段的计时不能约束之前的成本。

两个小文件反例：调用前 stop 已置位，仍发生 **两次完整哈希**；设置 `max_bytes=1` 检查 67 字节 PNG，总共读出 **209 字节**才给 `undecided/bytes_limit`。后者不是要求解析器连一个头字节都不能多读，而是证明“限制前已经完整读多遍”，真实大文件下会放大。

**反例**：`test_ux04_cancelled_quality_check_does_not_hash_the_whole_file`、`test_ux04_byte_ceiling_is_checked_before_full_input_work`。

**修复要求**：冻结预算作用域（整文件命令总预算，还是分阶段额度但另有总预算）；从第一个 I/O 前到最后证明都检查停止、字节、时间。已停止或 stat 已知超限时不启动完整哈希。receipt 的实际读盘字节/耗时不能只统计解码器。对反滤波长行的 CPU 取消也应加有界检查，实际延迟另测。本轮没有运行巨大图或解压炸弹。

## UX-05 · P2 · 事实采集到覆盖汇总的字段契约不一致

**位置**：`core/follow_facts.py:76`（特别是 :89）、`core/follow_coverage_counts.py:158`。

真实 `gather_facts()` 输出 `run.totals.artists_total`；counts 层只读 `total_artists`。传入已完成、total_artists=1 的 Source 与一位 works_seen=3 的画师，经 gather_facts→build_coverage 后，`artists_total=None`，`artworks_seen=None`。模块分别测试的手写字典没有暴露这条实际组合。

**反例**：`test_ux05_gathered_artist_denominator_survives_composition`。

**修复要求**：冻结一个带 schema/version 的事实 DTO；兼容别名若必要，在入口统一规范化并拒绝冲突，不让每一层自行猜。增加临时 CrawlStateStore→Source→gather_facts→coverage→文案的整链测试。不得用“缺失就 0”掩盖不完整输入。

## UX-06 · P2 · 作品、图片页、已有核验三种计数混用

**位置**：`core/follow_coverage_state.py:190`；`core/follow_coverage_counts.py:194`；展示消费 `follow_coverage_text.py`。

状态层用 `works_seen - excluded_by_filter <= pages_saved` 宣布完成；计数层却要求 `works_seen == pages_saved + excluded_by_filter`。两个式子既不一致，左右单位也不同。

反例一：1 个作品实际保存 3 页，计数被判为不一致。反例二：只提供 1 个作品和 1 页已保存、没有作品预期页集/交付证明，报告却给 `delivery=complete / overall=complete`。这不是证明实际丢了两页，而是证明它在没有区分单页与多页的证据时就宣告保存已齐。已有完整核验页不应因为本批新增保存为 0 而被当作未交付，现有公式也未表达这一点。

**反例**：`test_ux06_one_multipage_work_is_not_inconsistent_for_saving_three_pages`、`test_ux06_one_saved_page_does_not_prove_one_work_fully_delivered`。

**修复要求**：作品层分已见/排除/可下载/未知；页层分预期/新增/已有已核验/失败/待核验，并按内容及页标识去重。完成证明来自明确的当前可见范围与该范围内每个应保存页的有效交付证据。统计缺失应 unknown，不能拿另一个单位代替。补齐前禁止 UI 使用“保存与核验已齐”。

**范围**：当前真实来源尚未采集完整保存/筛选统计，报告为 unknown 是合理范围限制；本项针对新引擎已有的完成判断错误，不声称日常界面已经这样显示。

## UX-07 · P2 · 依赖报告在证据缺失或约束丢失时退出成功

**位置**：`tools/maintenance/dependency_matrix.py:101/:165`；`dependency_report.py:58` 起的组合及 main。

三个公开 CLI 反例分别为 requirements 引用不存在文件、core 中存在不可解析 Python、pip check 返回 1。环境盘点使用合成 fixture，声明与 import 解析运行真实代码；各自报告确实含对应错误，却都 **matrix.verdict=ok / exit=0**。错误附在详情里不等于最终门禁已处理它。

另一个矩阵反例：同一 runtime 包同时声明 `>=1` 与 `<2`，安装版本 2.5。`_declared()` 的“first group wins”连同一组后续约束也一起丢掉，只验证 >=1，于是错误通过。分组优先级不应意味着可以丢约束。

**反例**：`test_ux07_incomplete_dependency_report_cannot_exit_success` 的 3 个参数；`test_ux07_all_runtime_version_constraints_must_hold`。缺失 runtime 包为 attention 的正常对照通过。

**修复要求**：报告增加证据完整性总状态；解析失败、include 缺失、pip check/metadata 故障必须参与总 verdict/退出码；未检查和无问题分别表示。保留所有适用约束及来源、markers/extras/group；只有已证明不适用的条件才可排除。未知不应遮盖已知冲突；冲突与未决可同时报告。

**边界**：本轮没有安装、卸载、升级任何包，也没有声称完成实时漏洞库核验。共享 Conda 环境的回退不等于从 pip 列表重新安装；现有 rollback 文本只是收集材料的起点。

## P3 与接入前债务（不混入上述 P1/P2 计数）

1. **原因码来源重复**：`filter_explain.py` 自己复制映射/构造，未使用 `filter_reasons.py` 的冻结/截断实现；测试对齐不能自动让未来修改同步。`FrozenReason` 还漏掉普通 `|=` 运算：`r |= {'code': 'invented'}` 成功写入非法码。已用单独本地解释器确认；尚未证明会直接改变已发布任务，按 P3 处理，修成一个统一构造入口并补普通运算测试。
2. **新能力未交付到产品流**：U2-A/U2-B 是独立引擎；保存方案没有接到真实任务持久化与 UI；重评估尚无正式 CLI/UI 入口。U1-C 独立 decode-check 没接 scan，这是有记录的范围收缩，不应当作 bug，也不能在总表写全部交付。
3. **Tk 稳定性未关闭**：本次 34 项在按模块/用例隔离进程下通过。历史 teardown/Tcl 竞态原因仍未证明，不能把本次绿当成已修。
4. **目录与状态入口落后**：README 停留上轮 9ee6206，文档导航说旧缺口全关闭，IMPLEMENTATION 总表仍把 U1-B/C、U2-A/B 写待实施，TASK/LOG 缺本轮准确状态；AMD2 当前导航有重复目录链接。本轮修当前索引，不改历史报告原文。
5. **剩余验证边界**：真实外置卷、非本机文件系统、真实迁移/恢复及云端回读/释放仍未由本轮验收；43 个活跃 worktree、73 个已归档 worktree 中有 5 个活跃脏树需保全。目录多少不直接证明代码错误，禁止据名字清空。

## 对旧修复的公允判断

上一轮的 14 个原样反例在此产品版本 **14/14**；版本化归档工具测试 **23/23**。旧递归清理、前缀冲突、恢复检查假成功、刷新代次、归档身份组合等已有修复不能抹掉。UX-01 是所有权保护剩余的写入与 sidecar 邻接路径，WR-04 是仍漏出的失败边界；其余为新能力组合缺口。因此结论是“已有明显进展，但当前候选仍需修复”，不是“之前完全没修”或“全套已安全”。
