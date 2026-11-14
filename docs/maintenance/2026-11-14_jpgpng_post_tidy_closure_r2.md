# JpgPng post-tidy 复审修复轮（AMD2）维护记录（2026-11-14）

本轮处理[post-tidy 复审](../audits/2026-11-14-post-tidy-closure/README.md)的 **6 类缺口**（1 P1 + 5 P2）：CL-01、WR-04 剩余、CL-02、CL-03、WR-06 剩余、PA-03 剩余。被审候选是上一轮的 `9ee6206`，集成从审计头 `99494b1` 起，冻结条件见[契约 AMD2](../optimization/2026-11-14/U1A_MAINTENANCE_CLOSURE_CONTRACT_V6_AMD2.md)。

## 1. 起始事实（自己复现，不引用别人的数字）

| 项目 | 值 |
| --- | --- |
| 被审候选 | `9ee6206`（上一轮修复轮） |
| 集成基线 | `99494b1`（审计报告 + 上轮实现说明） |
| 复审 14 条反例 | **12 红 / 2 绿**（配 `PIXIVKEEP_ARCHIVE_COLLECTOR` 指向当时采集器）；不配时 PA-03 一条显式 skip |
| 既有套件口径 | 更正为 **2,158 → 2,263**（上轮"2,149 → 2,229，净增 80"混用了排除范围；上轮只排除 2 个展示文件，本轮多排除了 25 项 view） |

## 2. 逐项关闭

| 编号 | 根因 | 修法 | 行为证据 |
| --- | --- | --- | --- |
| **CL-01 P1** | `_discard` 枚举目录里当前存在的一切，遇目录递归；Windows junction 在 `is_dir(follow_symlinks=False)` 下仍是目录，于是递归删掉链接目标内容 | 新建 `core/workspace_ownership.py`：`TemporaryWorkspace` 记录**目录身份**与**每个成员身份**；`discard()` 只删仍匹配的已登记成员，重解析点/未登记/同名替换/不可读一律保留并报告；`_discard` 保留为两行委托（它是复审用例的注入点） | `test_cleanup_keeps_a_file_this_run_did_not_create`、`test_cleanup_refuses_a_junction_and_leaves_its_target_alone`、复审 `test_cl01_*` 两条 |
| **WR-04a P2** | `_write_manifest` 在失败边界之外，晚期 OSError 裸逃逸 | 从 `mkdir` 成功到 manifest 原子完成统一边界；晚期失败 `backup_manifest_failed`，detail 含主因与 `residual=` | `test_a_late_manifest_failure_is_typed_and_names_the_residual`、复审同名用例 |
| **WR-04b P2** | 清理异常覆盖复制主因 | `_safe_cleanup` 捕获清理异常，主错误先保存再附加 `cleanup=`；`KeyboardInterrupt/SystemExit` 保原类型 | `test_a_cleanup_failure_cannot_replace_the_primary_error`、复审同名用例 |
| **WR-04c P2** | 成员名含 `\x00` 走到 `Path.resolve` 抛 ValueError | 任何文件操作之前拒绝控制字符 → `manifest_member_invalid`、退出 3 | `test_a_member_name_with_a_control_character_is_refused_first`、复审同名用例 |
| **WR-04d P2** | 暂存区建不出来只加 reasons，CLI 仍返回 2 | `verify_backup` 返回 `exit_code`，暂存区不可建立 → 3 | `test_an_unavailable_staging_area_is_an_unreadable_exit`、复审同名用例 |
| **CL-02 P2** | restore-check 只看 missing 与 orphan WAL，损坏/空/不兼容一律 ok | 判定表：`unobservable`(3)、`library_missing`(2)、`not_usable`(2)、`wal_unreadable`(2)、`orphan_wal`(2)、`ok`(0)；`wal_bytes` 读不到写 `null` | `test_restore_check_*` 六例、复审三条参数化 |
| **CL-03 P2** | `complete_prefix = walk == full_walk and coverage != error`，预算中断也发证书 | 恒 `False`；`state` 四值（`not_needed/at_cursor/stopped/blocked`）；游标只表示"可以从哪继续" | `test_a_budget_stop_does_not_certify_a_prefix` 等 11 项新增、复审 `test_cl03_*` |
| **WR-06 剩余 P2** | 刷新不换查询代次，同批同游标的旧响应仍被接受，且不发新请求 | 新增 `_verification_generation`，刷新同步「代次++ → pop watcher → 清行/游标/摘要 → loading → 立即发新请求」；成功与错误回调都过 `(epoch, generation, batch)`；不再 `Future.cancel()` | 面板 86 项（含 `test_a_refresh_drops_the_page_reply_that_was_already_in_flight`）、复审 `test_wr06_*` |
| **PA-03 剩余 P2** | 只按已打开句柄定身份，路径身份未知被丢掉仍写 `identity=ok` | 采集器迁入 `tools/maintenance/`（版本化）；身份拆 `handle_identity/path_binding/content` 三分量，任一 unknown → 整体 unknown，已知冲突 → changed | `tools/maintenance/test_identity_capability.py` **21 项**；复审 `test_pa03_*`（指向新工具通过、指向旧脚本仍失败） |

## 3. 验证

| 范围 | 结果 |
| --- | --- |
| 复审 14 条（`tests/test_u1a_closure_boundaries.py`） | **14/14 全绿**（3.50 s，0 跳过——采集器已进仓库） |
| 上一轮 10 条边界 | **10/10** |
| 上一轮 WC 12 条 | **12/12** |
| 主体全量（排除 3 个 Tk 模块） | **2,295 项：2,291 通过、4 跳过、0 失败、0 错误**，665.93 s |
| `test_collection_view.py` / 两个 presentation 模块 | **25/25**（23.59 s）/ `test_collection_presentation` **4/4**（2.98 s）/ `test_source_presentation` **5/5**（3.37 s）。**这三个数是单次观测**：同配置同进程下 §8 记录过 10 次里 2 次失败，因此它们只证明“这一次跑通了”，不证明持续稳定 |
整套合计：**2,295（主体）+ 25 + 4 + 5 = 2,329 项**，2,325 通过、4 跳过、0 失败、0 错误；与复审口径的 2,263 相比净增 66（复审 14 条进入主体 14、生命周期 29、scan walk 11、面板 12；14+29+11+12=66 与套件差一致）。

## 4. 真实库与真实备份（只读）

驱动 `probe/real_rehearsal.py`，报告 `verify/real_rehearsal.json`。

| 步骤 | 结果 |
| --- | --- |
| 库字节与 `sha256` 前后 | **完全一致**：4,435,968 字节、`0cba55b8…4d3d` |
| `record_backups` 目录条目 | 11 个，前后**一字未增** |
| `inspect` | exit 0：`compatible`、49/49 对象、`quick_check/integrity_check=ok`、外键 0 |
| `verify record_backups/pre-w5w6-proof` | exit 0、`ok=True`、`exit_code=0`；`evidence.cleanup = {state: ok, removed: ["library.sqlite3"], kept: [], errors: []}`；隔离副本同级、复制 0.0077 s / 回读 0.0052 s / 读取 0.0686 s |
| `verify record_backups/pre-w5w6-switch`（v1） | exit 2、`manifest_too_old`（如实拒绝） |
| `scan` 真实库 | exit 0、`scope=full_walk`、`coverage=complete`、`pages_examined=1`、11 行全 match、95,574,715 字节 0.11 s；`resume = {state: not_needed, complete_prefix: false, exit_code: 0}` |
| 隔离副本残留 | 0（`record_backups` 父目录下无 `*.verify-*`） |

## 5. 与审计口径的三处登记

1. **统计口径更正**：上轮写的"2,149 → 2,229 净增 80"混用了排除范围，按整套应为 2,158 → 2,263（净增 105）。这是口径错误，不是少跑。
2. **`content` 三值**：AMD2 初稿把 `content` 写成 `ok|unknown`，实现按规则 5 需要 `changed`；已在 AMD2 内补成 `ok|unknown|changed` 并注明是本次修订内的口径补全。
3. **`_discard` 名字保留**：旧枚举式实现已彻底消失，但函数名保留为两行委托，因为复审用例用 `monkeypatch.setattr(m, "_discard", …)` 注入清理失败——改名会连带改审计证据，属于不允许的改动。

## 6. 只读复审（第二轮）与处理

对照 AMD2 逐文件审 `9ee6206..e5ab12d` 的只读复审结论：**P1 = 0**，方向成立；但抓到 **2 项 P2**（都在本轮主题上）与 8 项 P3，逐条处理如下。

| 编号 | 内容 | 处理 |
| --- | --- | --- |
| P2-1 | verify 的 adopt 竞态：`TemporaryWorkspace.adopt` 抛 `WorkspaceError`（RuntimeError），内层只 `except OSError`，逃到外层 `finally` 时 `ws` 未绑定 → `UnboundLocalError` 覆盖原始异常，`--json` 打出 traceback | 已修：`ws` 先置 None、内层捕 `(OSError, WorkspaceError)`、finally 守门；无法描述身份的目录写进 `evidence.left_behind` 并加 warning `verify_workspace_unowned`，`exit_code=3` 且 `status` 为 `error`。独立探针复现 → 修复后 exit 3、`ok=false`、无 traceback |
| P2-2 | 半途失败的副本文件在 `_copy_and_hash` 返回后才登记，失败时被当“陌生项”永久保留，错误也不提残留 | 已修：`claim()` 用 O_EXCL 创建并 `fstat` 记录、`release()` 撤掉从未写入的空成员；`_copy_and_hash` **签名不变**（复审原件的注入点），改为打开已存在文件写入；清理以 (dev, ino) 为删除凭据，inode 相同但 size/mtime 变化按“半成品”删除并在 `incomplete` 里列出 |
| P3-1 | `IMPLEMENTATION.md` 有两个 `## 4O` | 已修：审计节移到 §5 前改名 **4P**，顺序为 4N→4O→4P→§5 |
| P3-2 | 记录里“生命周期 25”实为 24 | 已修（14+24+11+9=58 与套件差一致） |
| P3-3 | Tk 的 4/4、5/5 被当成稳定结果 | 已修：标注为**单次观测**，并指向 §8 的 10 次样本 |
| P3-4 | AMD2 的 restore-check 表把 `missing` 排在 WAL 之前，与实现相反 | 已修：表按实现真实顺序重排（WAL 不可读在最前） |
| P3-5 | `_safe_cleanup` 的 `except BaseException` 吞掉清理期间的 Ctrl-C | 已修：只捕 `Exception`；取舍写明——清理期间的中断按原类型上抛并覆盖主错误，因为操作者已要求停止 |
| P3-6 | 归档工具在“句柄 unknown + 路径已知冲突”时把冲突遮成 unknown | 已修：**可测量的冲突优先于 unknown**（`path_binding=changed`、`identity=changed`）；无可比较的一对时仍是 unknown；README 与 docstring 同步 |
| P3-7 | 页读失败没清 `_verify_read_revision` | 已修：新代次与两种失败各自清空对应版本号，补 3 条回归（含“真报警仍要报”的反向断言） |
| P3-8 | 同一句注释重复两行 | 已修 |

复审自己跑的数字：251 项全绿（closure 14、lifecycle 24、backup 26、restore 6、walk 52、panel 86、post-tidy 10、w5w6 12、tools 21），并独立核过真实备份 `verify` 与库 `sha256`。

### 6.1 增量复审（e5ab12d..e1da73a）

复核上述 2 P2 + 8 P3 的修复本身：**P1 = 0、P2 = 0**，判「可以合并进候选」，并另列 4 项小 P3，已全部修（`59f74c4`）：

| 项 | 内容 | 处理 |
| --- | --- | --- |
| a | `docs/README.md` 页首仍写 2,321/2,287，与记录矛盾 | 已改为 2,329/2,295 |
| b | `restore_check` 里 `if not wal_readable` 分支不可达（前面已 return） | 已删除并留一行注释 |
| c | 成功的 verify 也带 `"status": null`，消费者可能误读成错误 | 已改为只在 `exit_code == 3` 时写 `status`；真实备份实测 `ok=True / exit_code=0 / has_status=False / cleanup=ok` |
| d | `release()` 先 pop 后 unlink，unlink 失败时成员失去登记 → 之后被当陌生项 | 已改为 unlink 成功后再 pop；失败保留登记，仍按半成品报告 |

增量复审自己跑的数字：closure 14、lifecycle 29、tools 23、panel 89 全绿，`pytest tests --collect-only` 得 2,295，与记录一致。
## 7. 明确未做与风险

- **Tk 拆除稳定性（既有 P2）未闭环**：本轮只攒了数据（见 §7），没有根因结论。
- **真实库恢复未执行**；`tools/maintenance` 的 `apply()` 只在合成批次跑通，真实批次未演练；跨卷身份变化未实测。
- **旧采集器仍在 `.runtime/workspace_tidy_20261114_01/`**，行为仍旧。今后只允许用 `tools/maintenance/resume_archive.py`；旧脚本保留历史、不改。
- `TemporaryWorkspace.record` 在"创建完成"与 `lstat` 之间有极小的 TOCTOU 窗口；彻底消除需要把创建改成由 workspace 持有的句柄，本轮未做。
- 晚期备份失败**允许**留下自有残留（detail 写明 `residual=`），因为不允许为清空目录而扩大删除权限。
- `restore_check` 先看 WAL 再看库：库缺失且 WAL 读不到时报 `wal_unreadable` 而不是 `library_missing`（docstring 已写明顺序与理由）。
- Windows 上 mount point 也被当重解析点保留（安全侧，不自动清理）。
- 清单摘要不是签名；`verify` 的隔离副本是显式代价。

## 8. Tk 稳定性数据（不结论）

| 实验 | 结果 |
| --- | --- |
| 同进程连续 5 次 `Tk()` 创建/销毁，plain / `gc.collect()` / 保留引用三模式，各 5 轮 | **15/15 全通过** —— "多根必然崩"不成立 |
| 两个展示模块放同一进程，连跑 10 次 | **8 通过 / 2 失败**，失败点都在后续用例 `setUp` 的 `Tk()` 初始化（`TclError`） |
| 上一轮在候选 `9ee6206` 上同法 3 次 | 6 通过 / 3 失败；未改动基线 `4ba891e` 单模块 3 次为 1 通过 / 2 失败 |

结论只到"间歇性、与本轮改动无关、触发条件不是根的数量"；根因仍待专项诊断（下一轮 S5）。

## 9. 回退

`git checkout fix/a1-post-tidy-closure-20261114`（上一轮候选 `9ee6206`），或退到标签 `checkpoint-2026-11-14-post-tidy-closure`。日常目录本轮不动，仍为 `4ba891e`。
