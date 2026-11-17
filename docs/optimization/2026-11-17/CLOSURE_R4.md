# R4 小修关闭报告（两项边界 + 验证门槛）

锚点：实现 `7e657cb`，审计 `3f5f141`（`audit/ra-r3-followup-20261117`，未推送）。
修复分支 `fix/ra-r4-20261117`；契约 [S0_CONTRACTS_R4.md](S0_CONTRACTS_R4.md)（含 §9 修订记录）。

## 结论

- R4-01（WAL 私有副本重试漏已提交数据）与 R4-02（`ensure_current` 从旧无账号 scope 推导账号）都已修，各有「修前红 → 修后绿」证据。
- R4-03（验证脚本永远退出 0）已修：现在任一组出现缺 XML、XML 不可解析、非预期失败/错误、预期失败未失败、测试数减少，主进程非零，并写出机器可读 `matrix_summary.json`。
- R4-04（源旁车措辞）已按审计建议收窄：字段保留，文本改成「本次运行期间出现，因果归属未完全证明，未删除」。
- **六包不重写、旧断言不降低、旧反例不删**；历史相邻 13 条仍是 **12 绿 + 1 条具名过期失败**，这一条在 R3 已被证明与其兄弟反例互斥（§R3 10.1），本轮不改口。
- **生产面仍然全部未做**：真实库恢复/释放、真实账号写回、云盘往返、部署都没有执行。

## 逐项状态

| 编号 | 契约 | 实现 | API 整链 | UI/CLI | 真实生产 |
| --- | --- | --- | --- | --- | --- |
| R4-01 私有副本重试 | 已冻结（§1） | 已提交 `f0d7faf` | 已验（V2 CLI、legacy CLI、DesktopRuntime 各一条） | 已验（`coverage` 的 `read.plan.copy` 新键） | 未做 |
| R4-02 禁止推导账号 | 已冻结（§2，§9.1 按冻结反例修订） | 已提交 `af09868` | 已验（`ensure_current` 五态 + 报告 proof_limits） | 未接（无生产调用方） | 未做 |
| R4-03 验证门槛 | 已冻结（§3） | 已重写 `verify/run_round.py` | 已验（红：2 条非预期失败 → exit 1；绿：仅有具名失败 → exit 0） | 不适用 | 不适用 |
| R4-04 旁车措辞 | 已冻结（§4） | 已提交（同契约提交） | 已验（3 条用例 + 51 项回归） | 已验（`maintain backup` 文本） | 未做 |

## R4-01 · 私有副本重试必须按每次 attempt 重建（P2）

- 位置：`core/follow_facts_v2.py` 的 `_private_copy`。
- 修法：每次 attempt 重新观察源文件族，成员清单（主文件 + 当时存在的 `-wal`）由**该次**观察得出；每次 attempt 用独立子目录 `<私有根>/attempt-<n>/`，成功那次才把成员换到私有根，其余删掉；重试同步更新 `before/after/difference`；`plan.copy` 取最终 attempt，新增 `attempts` 与 `dropped_members` 做证据；达上限 → `source_changed_during_copy`，不给任何数字；`verified` 只在当前 attempt 完整时成立。
- 已证明（两段，分开记）：
  1. 审计反例：`runs/a/before.xml` 1 failed（`copied_rows=[1]`、members 只有主文件、`verified=True`）→ `runs/a/after.xml` 1 passed（`copied_rows=[1,2]`、members 含 `-wal`、`attempts=2`）。
  2. 复审追加项（成员在两次观察之间消失）：修前是 `private_copy_failed` 且只有 1 次复制；修后是**重试成功**——`rows=[1,2]`、`attempts=2`、`members=[main]`、`dropped_members=[wal]`。对照脚本用同一进程并排加载修前/修后模块，输出在 `runs/a`。
  两段合计 15 条新用例（初始无 WAL / crash 形状 / WAL 被 checkpoint 掉 / 主文件消失 / 真 I/O 失败仍算复制失败 / 两趟都变 / 取消 / 复制异常 / 连接打开异常 / 三条原选择表 / V2 CLI / legacy CLI / DesktopRuntime）；合流后定向回归 176 项 0 失败；源主文件在重试与读取期间字节不变。
- 仍未知：一致性仍只看名字与 `(size, mtime_ns)`，**同尺寸改写并精确恢复 mtime 的 ABA 逃得过去**；两趟之间无可观察变化而内容被换掉看不出来；`-wal` 在观察与复制之间被删时报 `private_copy_failed`（保守取「读失败」）；未做超大库长跑（R3 的 150 MB 测量仍适用）。

## R4-02 · `ensure_current` 不得推导账号（P2）

- 位置：`organizer/group_scope.py` 的 `ensure_current`。
- 修法：旧 scope 无账号、新快照有账号时，返回**身份不变**的对象（`account_id=""`、`account_id_state="not_recorded"`），成员/blocks/completeness/新 `snapshot_id` 照新快照采纳；`identity_fingerprint` 从去掉账号的 `source` 重算；不新增错误码、不新增 source 键、`GroupScope` 字段集合不变。
- 已证明：反例转绿（`upgraded.account_id == ""`、报告保持 `account=unknown`/`complete=false`）；14 条新用例覆盖五态、同标签同成员不同账号、双未知、成员变化、证据降级、序列化往返、CLI 退出 5；232/232 与 202/202 回归 0 失败。
- 仍未知：`ensure_current` **仍没有生产调用方**（只有测试），所以不能说成已发生跨账号写回；返回对象带刷新后的 `snapshot_id`，把批次钉在旧 snapshot 上会被 `build_report` 以 `snapshot_mismatch` 拒绝——这条**在修改前就存在**（旧实现同样返回 fresh），不是本轮引入。
- **偏差记录**：开发指导的推荐裁决是「抛 `scope_account_requires_rebind` 要求重新 build」，但冻结反例要求 `ensure_current` 正常返回且账号为空；冻结反例是验收物，故按它实现，`scope_account_requires_rebind` 这个码**不存在**。将来接 CLI/续跑时若要显式拒绝入口，应检测 `account_id_state == "not_recorded"` 另加（记入遗留）。

## R4-03 · 验证门槛必须能失败（P2，工具）

- 位置：`.runtime/jpgpng_ra_r4_fix_20261117_01/verify/run_round.py`（工具，不入产品代码；sha256 `1dab346e…` 见 RESULTS_R4）。
- 修法：每组记录 command/worktree/HEAD/worktree 状态/起止时间/returncode/XML 路径/XML 完整 SHA-256/计数/具名预期失败与其原因；判定条件为缺 XML、XML 不可解析、error、非预期失败、预期失败未失败、returncode 异常、测试数**减少**；任一成立 → 主进程非零并写出 `matrix_summary.json`。
- 已证明：红——baseline 上 `r4` 组 2 条非预期失败 → exit 1 并列出 node 名；绿——历史相邻组只允许那一条具名失败时 exit 0；R3 旧脚本在同样输入下会 return 0。
- 仍未知：脚本在 `.runtime`，不在 Git 里（与 R3 一致）；它现在会把自己的 sha256 写进每次的 `*_summary.json`，用户复核时以该 JSON 里记的路径与哈希为准。
- 一个环境事实被写进了门槛模型：本机 Tk 在同一进程里反复初始化会间歇失败（`Can't find a usable tk.tcl` / `ttk/scrollbar.tcl` 读不到；R3 工作树上同样复现，与本轮 diff 无关）。presentation 因此拆成两组：`pres`（**每用例一个进程**，9/9，作为门槛）与 `pres_together`（同进程一次跑，**只记录不设门槛**，本轮 8 passed + 1 error 原样留档）。

## R4-04 · 旁车措辞（P3）

- 字段 `source_sidecars_created` 保留（manifest 不升版本），文本与 docstring 改为「本次运行期间出现；与本次读取的因果归属未完全证明；未删除」。
- 不借这条措辞删除源旁车；`tests/test_ra01_source_bookkeeping.py` 的用例名与说明同步改成 appeared 语义，断言不变。

## 历史相邻 13 条的裁决（不变）

`test_ra04_visual_receipt_cannot_be_reused_under_new_rule_version` 继续失败：它断言「store 里没有 review 也 sufficient」，与同文件另一条互斥（R3 §10.1）。本轮**没有**为了全绿去恢复对未落库字典的信任，也没有把该文件改成预期失败清单以外的形态；验证门槛把它列为**具名**预期失败，出现任何额外失败即非零。

## 验证矩阵（代码与测试的最后提交是 `f0ad380`；矩阵在其后运行）

```
组                tests  passed  failed  errors  skipped  XML                          sha256[:24]
r4                    4       4       0       0        0  r4final2_r4.xml              6af75c1ec9555b037bfe3f27
adjacent             13      12       1*      0        0  r4final2_adjacent.xml        93b28164362019dd7a3ca4fe
canonical            19      19       0       0        0  r4final2_canonical.xml       dc51a058e720f2484f4a3f5b
frozen               15      15       0       0        0  r4final2_frozen.xml          7c6a171e5462caafac354973
body               3587    3582       0       0        5  r4final2_body.xml            626688cf803c1364efe5b96a
tk                  118     118       0       0        0  r4final2_tk.xml              7993b566e3ffe01c16f8643d
pres (逐用例进程)      9       9       0       0        0  r4final2_pres/*.xml（9 个）   每个用例一份 XML
pres_together         9       7       0       2        -  r4final2_pres_together.xml  c645145be3e876430fd7ba1a
```

\* 具名预期失败：`test_ra04_visual_receipt_cannot_be_reused_under_new_rule_version`（R3 §10.1 的过期反例）。
`pres_together` 是**只记录、不设门槛**的组：本机 Tk 在同一进程里反复初始化会间歇失败（见 R4-03 一节），
失败的 2 条原样留在该 XML 里；门槛看的是 `pres`（每用例一个进程，9/9）。

合计：主体 **3587**（含 canonical 19 与两个新反例模块）+ Tk **118** + presentation **9** = **3714 项**，
**3709 通过 / 0 失败 / 0 错误 / 5 跳过**；树外另列：旧 15 冻结 **15/15**、历史相邻 **12/13（1 条具名）**、R4 反例 **4/4**。

运行器 `verify/run_round.py` 的 sha256 是 `c1daca7ebf7e2f1b1596b470c1fc7296d2dda57b7644d9d33d7a958487add056`（每次运行也会写进 summary 的 `runner.sha256`）。
矩阵运行时工作树带着未提交的文档改动（CLOSURE_R4 / REVIEW_R4 / 维护记录 / TASK_PLAN / RESULTS_R4），summary 里 `worktree_dirty=true` 已如实记录；代码与测试自 `f0ad380` 起未变。

## 仍未闭合 / 之后排期（本轮明确不做）

1. 把 `maintain inspect/verify` 与 `ReadOnlyCrawlState` 的无参入口纳入统一读计划；备份在线 API 不能机械换成静态副本。
2. 先决定审阅/计划命令的 canonical `RuleSet` 来源，再让 `samples.review` 拒绝无指纹新记录、`plans.make_plan` 校验指纹；旧记录保持 unknown。
3. V2 作品页期望内容代次是数据面设计（来源、事务、代次/重试语义、旧数据 unknown、备份回退、迁移测试），做完才谈 DDL；禁止把 observed SHA 填进 expected。
4. 外部 WAL/SHM 回收、真实恢复、外置卷/云盘、真实账号写回、生产部署全部另批，未获明确授权不做。
5. `ensure_current` 的显式拒绝入口（见 R4-02 偏差记录）等接线时再决定。

## 复审

见 [REVIEW_R4.md](REVIEW_R4.md)（本轮只读 fork 的逐条裁决与处理）。