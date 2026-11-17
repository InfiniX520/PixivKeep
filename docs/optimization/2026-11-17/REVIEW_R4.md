# R4 只读复审与处理

复审者是继承本轮上下文的只读 fork（`review-r4`），不改已跟踪文件、不碰真实数据。
原始意见：`.runtime/jpgpng_ra_r4_fix_20261117_01/verify/REVIEW_R4_raw.md`（10,392 B）。

## 复审自己复跑的结果

| 检查 | 结果 |
| --- | --- |
| `run_round.py … r4 adjacent canonical frozen` | r4 **4/4**、adjacent **12/13**（唯一失败是具名过期反例）、canonical **19/19**、frozen **15/15**，exit 0 |
| 16 个模块定向回归 | **261 passed / 0 failed** |
| 自己造门槛失败（8 个场景） | 通过→0；非预期失败→1；预期失败未失败→1；缺 XML→1；计数减少→1；fixture error→1；零用例→1；垃圾 XML→解析失败 |
| 冻结边界 | `git diff 3f5f141..HEAD -- docs/audits tests/test_boundaries.py` = 0 个文件 |
| 测试改动 | 只有契约点名的两处，无第三处 |

## 处理结果

| 编号 | 复审意见 | 处理 |
| --- | --- | --- |
| P1-1 | 三处 `<!--MATRIX-->` 占位与 `REVIEW_R4.md` 不存在 | **已补**：本文件 + CLOSURE_R4 / 维护记录 / RESULTS_R4 的矩阵按 `r4final2_*` 的 XML 实际值填 |
| P2-1 | 成员消失被当成「读失败」，注释与行为相反（`private_copy_failed`，重试没发生） | **已修** `50737b5`：成员消失 = 变化 → 丢弃本次 attempt 重试；主文件消失两趟都没有 → `source_changed_during_copy`；`private_copy_failed` 只留给真 I/O 失败。修前/修后用同进程并排加载模块对照：修前 `private_copy_failed, attempts=null`，修后 `rows=[1,2], attempts=2, members=[main], dropped_members=[wal]`。新增 3 条用例 |
| P2-2 | `RESULTS_R4.md` 记的脚本哈希与实际不符 | **已修**：脚本改为把自身 sha256 写进每次 `*_summary.json`；RESULTS_R4 的哈希按最终版本重算 |
| P3-1 | 计数减少时 verdict 仍是 `ok` | **已修**：verdict 改为 `count_decreased` / `skips_increased`，两者都会让主进程非零 |
| P3-2 | 跳过没有被门槛覆盖 | **已修**：每组记录 `EXPECTED_SKIPS`，跳过数上升即非零 |
| P3-3 | 重试后 `read.source.note` 像在说整段复制期间源没动 | **已修** `50737b5`：`attempts > 1` 时 note 补「这份结论来自最终第 N 次尝试的观察窗口，前 N−1 次因源在变被丢弃」 |
| P3-4 | `TASK_PLAN.json` 的 `fixed_commit` 仍是 `null` | **已修**：指向本轮合流后的最终提交（见维护记录与计划文件） |

## 复审之外、由本轮环境暴露的一件事：Tk 的 presentation 组

最终矩阵第一次跑（`r4final_*`）时 `pres` 组出现 1 个 error：
`_tkinter.TclError: Can't find a usable tk.tcl … couldn't read file …/ttk/scrollbar.tcl`。
逐项排查：

- 同一条 `tkinter.Tk()` 在 `python -c` 与最小 pytest 文件里都能起（`root ok`），文件存在且可读；
- 在 **R3 的工作树**上跑同样的两个模块也失败 → 与本轮 diff 无关；
- 单条用例单独跑能过，整模块跑会在第 N 个 `Tk()` 处失败，且失败位置每次不同 → 同一进程里反复初始化 Tk 的资源问题（R2 起就记录过同类现象）。

处理：验证脚本把 presentation 拆成两组——`pres`（**每个用例一个进程**，作为门槛，9/9）与 `pres_together`（同进程一次跑，**只记录不设门槛**，本轮记到 8 passed + 1 error）。
这样既不把环境缺陷伪装成通过，也不让它掩盖产品结果；单进程失败仍原样留在 `r4final_pres_together.xml` 里。

## 复审对代码的结论（保留要点）

- R4-01 主路径真的修好了：每趟独立观察 + 独立子目录 + 成功那趟提升 + 失败清目录，`-shm/-journal` 仍不复制，源字节不变。
- R4-02 只去掉账号，`GroupScope.__init__` 会重算 `account_id_state`/`identity_fingerprint`，不会出现「state=recorded 但账号空」的自相矛盾对象，`source` 键集合与正常 scope 一致。
- `scope_account_requires_rebind` 在代码里 **0 命中**（只有文档解释它为何不存在），与契约 §9.1 一致。
- 文档没有把 R4-02 写成「已防住跨账号写回」。