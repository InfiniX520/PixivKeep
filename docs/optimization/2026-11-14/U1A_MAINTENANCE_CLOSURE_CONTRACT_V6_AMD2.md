# U1-A 维护闭环契约 v6 修订 AMD2：所有权、失败协议与证明口径

基线：`99494b1`（产品 `9ee6206` + 独立复审报告）。[契约 v6](U1A_MAINTENANCE_CLOSURE_CONTRACT_V6.md) 与[修订 AMD1](U1A_MAINTENANCE_CLOSURE_CONTRACT_V6_AMD1.md) 继续有效；本文件只补[复审 FINDINGS](../../audits/2026-11-14-post-tidy-closure/FINDINGS.md) 的 6 类缺口与相邻边界，不重开已关闭编号，不授权真实恢复、真实归档或联网。

## 0. 五条不变量

1. **看见文件不等于拥有它。** 清理权限只来自"本操作创建记录 + 此刻仍匹配的身份"，不来自目录名、时间、扩展名或路径前缀。
2. **无法观察 ≠ 已观察到不符 ≠ 通过。** 三者必须有各自的 type 与退出码。
3. **返回游标不等于已证明前缀。** 没有证明链时 `complete_prefix` 恒 False。
4. **刷新必须建立新读取代次。** 丢弃旧回调不等于回滚已提交的写任务。
5. **句柄身份、路径绑定、内容摘要分开表达。** 任何一项未知都不能被另一项的"正常"补成"已证明"。

## 1. 临时成员所有权（CL-01）

### 1.1 模型

新增 `core/workspace_ownership.py`，对外只有一件事：**创建时登记、清理时复核**。

```text
TemporaryWorkspace.create(path, *, purpose) -> ws
    # 目录创建后立刻记录目录身份：st_dev, st_ino, st_mode, 重解析属性
ws.record(name) -> None
    # 仅对本操作刚创建的成员调用；记录 (name, kind, dev, ino, size, mtime_ns, attributes)
ws.discard() -> {"state": ok|partial|kept|failed, "removed": [...], "kept": [{"name", "reason"}], "errors": [...]}
```

### 1.2 清理规则（逐条可验收）

- **只遍历已登记成员**，不 `scandir` 后"全部收编"。未登记项一律保留，`reason="unowned"`。
- 目录身份先复核：dev/ino 与创建时不符、目录本身变成重解析点、或读不到 → `state="kept"`，一个成员都不动。
- 每个成员删除前 `lstat` 复核 dev/ino/size/mtime；任一不符 → 保留，`reason="replaced"`。
- **任何重解析点都不进入、不删除**：`st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`（Windows）、`stat.S_ISLNK`、`is_junction()` 任一为真即保留，`reason="reparse"`。不使用 `resolve()` 后再判断"目标在根内"这种一次检查。
- 只删本操作登记过的普通文件与（空）子目录；**不递归删除未登记目录**。
- 删除该目录本身前必须确认它已空且身份未变；否则保留并报告。`state` 与 `removed/kept` 必须自洽：`kept` 非空时不得是 `ok`。
- **身份不可证即不可删**：`st_dev` 或 `st_ino` 任一为 0（某些网络卷/文件系统不报告 inode）时，该成员一律保留并记 `reason="identity_unavailable"`；目录身份不可证时一个成员都不动。
- **“半成品可删”的前提是对象已可证**：inode 相同、只有 size/mtime 变化时才按半成品删除（并在报告 `incomplete` 里列出）；inode 不可证时半成品也保留——宁可留残留，不删不确定的东西。不得用“size/mtime 完全一致就删”兜底。
- 成员在**创建那一刻**登记（`claim()` 用 O_EXCL 创建并 `fstat` 记录），不是复制成功之后；复制失败留下的半成品因此仍属本操作，清理能删或至少如实报告。
- 任何 `OSError` → 记入 `errors`，`state="partial"` 或 `failed`；**绝不向上抛成主错误**。

### 1.3 调用点（三个都必须换）

| 调用点 | 现在 | 改后 |
| --- | --- | --- |
| 备份复制失败 | `_discard(target)` | `ws.discard()`，结果进 `backup_failed` 的 `cleanup` 字段 |
| 备份校验失败（`problems`） | `_discard(target)` | 同上，`backup_verification_failed` 带 `cleanup` |
| verify 的 finally | `_discard(workspace)` | `ws.discard()` → `evidence.cleanup = {...}` |

`_discard` 这个"枚举目录"的旧函数必须消失（不再有任何调用者）。备份目标的 `library.sqlite3`、`-wal`、`-shm` 与 verify 副本的每个成员，在创建后立即 `ws.record(name)`；**SQLite 自己生成的 sidecar 也必须显式登记**，不得按后缀通杀。

### 1.4 报告与结论分离

- verify 报告新增 `evidence.cleanup = {state, removed, kept, errors}`；`ok` 仍只由 sealed/logical/health/compatible 决定。
- 文本：`清理: ok（删除 N 项）` / `清理: 保留 M 项（unowned/reparse/replaced）`；保留时附一行说明"核验结论不受影响，残留位置见 JSON"。
- `cleanup.state != "ok"` → warning `verify_cleanup_incomplete`，**不改变退出码**（除非同时有别的失败）。

## 2. 备份失败协议（WR-04）

### 2.1 统一边界

从 `target.mkdir(...)` **成功**那一刻起，到最终 `manifest.json` 原子替换完成，所有阶段（复制、fsync、源组复核、counts/definitions、copy inspect、逻辑指纹、清单序列化/写临时文件/flush/replace）都在同一个资源/失败边界内。

- 主错误先保存；随后尝试 `ws.discard()`；清理结果作为 `cleanup` 附加，**不得覆盖主错误**。
- 主错误映射：可预期的文件系统/序列化故障 → `BackupError("backup_manifest_failed"|"backup_failed", detail)`；`KeyboardInterrupt`/`SystemExit` 保持原类型原样抛出。
- 禁止 `except BaseException: return {ok: False}` 之类吞掉控制流。
- 失败时**不要求删光**：允许保留自有残留，但必须在错误 detail 里写清"残留位置 + 保留原因"（`residual=<path>`）。

### 2.2 输入阶段

- 成员名在**任何文件操作之前**校验：`\x00` 与其它控制字符、分隔符、`.`/`..`、绝对路径、盘符、超长 → `manifest_member_invalid`，退出 3。
- 非法 `mode`/`bytes`/`sha256`/`sealed_members` 继续走既有 `manifest_*`。
- **暂存区不可建立属于"执行条件不足"**：`verify_backup` 报告 `ok=False` + `exit_code=3`，CLI 必须返回 3（不是 2）。

### 2.3 退出码

`verify_backup` 的返回 dict 增加 `exit_code`（0/2/3），CLI 用它决定返回值，不再 `0 if ok else 2`。新增到"3"集合的错误码：`manifest_member_invalid`、`verify_workspace_unavailable`、`backup_manifest_failed`。

## 3. restore-check 判定表（CL-02）

先有表，再有代码。`status` 来自 `inspect_library`。

| 条件（按顺序，**实现里的真实顺序**） | state | 退出码 |
| --- | --- | --- |
| WAL `stat` 失败（有路径但读不到） | `wal_unreadable` | 2 |
| inspect 抛错 / 读不到 | `unobservable` | 3 |
| `status == "missing"` | `library_missing` | 2 |
| `status in {"unreadable","empty","incompatible","not_a_sqlite_file"}` | `not_usable` | 2 |
| `status == "compatible"` 且非 WAL 模式且 WAL 非空 | `orphan_wal` | 2 |
| 其余（compatible + 组一致） | `ok` | 0 |

- 报告保留 `status/journal_mode/header_journal/wal_bytes/reasons`；`wal_bytes` 读不到写 `null` 而不是 0。
- 文本分别说明"内容健康""当前版本可打开""文件组一致""未能观察"，不合并成一句"可用"。
- 不声称证明了任意 WAL 的归属；header 能证明什么、不能证明什么写进 docstring。

## 4. 扫描前缀证明（CL-03）

`scan_library_report(...)` 的 `resume`：

```text
{"state": "not_needed"|"at_cursor"|"stopped"|"blocked",
 "complete_prefix": false,        # 恒 False，直到有真正的证明链
 "exit_code": 0|2|3|5}
```

- `complete_prefix` **在没有任何证明链实现时恒为 False**，包括 `coverage="complete"` 的成功全程（它是"这次走完了"，不是"下次可以从这里证明前缀"）。
- `next_cursor` 只表示"可以从哪继续"。
- 不新增持久 resume、DDL、全局索引或缓存；未来的证明链另立设计（库身份、版本、策略、根映射、前缀摘要、断点连续性）。
- 退出码与严重度单调规则不变。

## 5. 查询代次（WR-06 剩余）

- 新增 `_verification_generation`，与批次 `_verification_epoch` 正交。
- **建新代次**的四个入口：进入新作业/批次、点击刷新、重开同一页、回到同一 cursor。
- 刷新（`reset=True` 或显式刷新）必须**同步**完成：generation++ → `_pending.pop("verification_pages"/"verification_summary")` → 清空行/游标/选择 → `state="loading"` → 立即发新请求。
- summary 与 pages 的成功/错误回调都校验 `(epoch, generation, batch)`；任一不符即丢弃，且**不得**把状态改成 ready/error。
- `(batch, cursor)` 不作为代次：它们可以重复。
- 不 `Future.cancel()`；生产者对已取消 Future 的行为未核实时，靠门禁而不是取消。
- 刷新**不撤销**已提交的恢复写任务，也不丢其持久回执；刷新后提示"已重新读取，之前的恢复结果请看新事实"。
- 一次读取代次同时清空**摘要区**：新读发出时旧摘要不再显示（它不是可操作行，但同样是旧事实）。
- 恢复写任务正在进行时点刷新：提示“正在核验，请等这一轮结束”，不打断该轮；该轮结束时由它自己发起的重读开启下一代次。这是对“刷新立即发新请求”的显式例外，避免在读取代次里替换正在工作的行。
- 面板仍为单页 50 行、上一页/下一页、无跳页；`read_revision` 不保证同快照的提示保留。

## 6. 归档身份三分量（PA-03 剩余）

`resume_archive.py`（迁入 `tools/maintenance/` 版本化管理）的 `file_record` 返回：

```text
{"bytes": int, "sha256": str|null,
 "handle_identity": "ok"|"unknown"|"changed",
 "path_binding": "ok"|"unknown"|"changed",
 "content": "ok"|"unknown"|"changed",
 "identity": "ok"|"unknown"|"changed",   # 组合结果
 "identity_reason": str|null,
 "volume"/"file_id"/"hardlink_count": int|null}
```

组合规则（优先级从上到下）：

1. 句柄前后任一 unknown，或最终 `lstat` unknown → `identity="unknown"`，`path_binding="unknown"`（**即使句柄正常**）。
2. 句柄前后有效但不一致 → `handle_identity="changed"`，`identity="changed"`（**即使路径未知**）。
3. 句柄与路径都有效但不一致 → `path_binding="changed"`，`identity="changed"`。
4. 三者都有效且一致 → `identity="ok"`。
5. 大小/mtime 在哈希期间变化 → `content="changed"` → `identity="changed"`（原 `size_changed_during_hash` 语义保留为 reason）。
- `content` 的三个取值就是 `ok/unknown/changed`（本文件初稿只写了前两个；S4 实现按规则 5 需要第三个，属本修订内的口径补全）。

`None` 不得同时表示"匹配"与"无证据"：未知一律显式字符串 + reason。`compare_files` 的双侧缺摘要规则保持（`sha256_missing` 计 `unproven`）。

## 7. 验收与口径

- 复审 14 条（`tests/test_u1a_closure_boundaries.py`，原件逐字节承接、仅 ROOT 一行调整）：目标 **14/14 绿**；红基线为 12 红 / 2 绿。
- PA-03 一条需要 `PIXIVKEEP_ARCHIVE_COLLECTOR` 指向 `tools/maintenance/resume_archive.py`；未设置时显式 skip，不得改断言。
- 全量口径固定三组：主体（排除 `test_collection_view.py`、`test_collection_presentation.py`、`test_source_presentation.py`）+ view 25 + presentation 9；三组不重叠、分开报。
- 危险注入只在本批合成目录内；每例断言两端绝对路径位于本批根内，并记录哨兵文件前后字节/摘要。
- 上一轮已关闭的 PA-01/PA-02、WR-01..05、WR-07 不得回退；`tests/test_u1a_post_tidy_boundaries.py` 与 WC 12 条继续绿。

## 8. 明确不改变

五份冻结协议、库 schema `"1"`、无新依赖、面板风格与无缩略图策略、`_VERIFY_PAGE_SIZE=50`、bucket/tone 语义、`compare_library.ok` 不含 sealed/source_equivalent、真实图库与 `record_backups` 内容不动、Tk 根因另案（S5）。
