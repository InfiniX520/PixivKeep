# 2026-11-17 R4 边界小修记录（R4-01…R4-04）

实现锚点 `7e657cb`，审计 `3f5f141`（`audit/ra-r3-followup-20261117`）。修复分支 `fix/ra-r4-20261117`。
本轮只做两处边界修复、验证门槛与一处措辞，不重写六包、不做 schema 迁移、不碰真实库/账号。

## 做法

1. **先冻结契约**：[S0_CONTRACTS_R4.md](docs/optimization/2026-11-17/S0_CONTRACTS_R4.md) 写清两项不变量、四态表、必测矩阵与门槛规则；开工前先在审计提交上复现 **2 红 / 2 对照绿**。
2. **两条 lane 文件不相交**：R4-A 只碰 `core/follow_facts_v2.py`，R4-B 只碰 `organizer/group_scope.py`；主智能体重写验证脚本、收窄措辞、写文档与导航，并做合流与复审。
3. **一处契约按冻结反例改写**：R4-B 的开发指导推荐「抛 `scope_account_requires_rebind`」，但冻结反例要求 `ensure_current` 正常返回且账号为空，两者不能同时满足；冻结反例是验收物，故按它实现（契约 §9.1 记录这次改动，关闭报告里也写明没有该错误码）。

## 逐项结果

| 编号 | 关键改动 | 红 → 绿 |
| --- | --- | --- |
| R4-01 P2 | 每次 attempt 重新观察成员、独立子目录、重试同步更新全部证据、`plan.copy` 取最终 attempt（新增 `attempts`/`dropped_members`）、达上限拒绝 | `-k r4_01` 1 failed → 1 passed；冻结文件由 2/4 → 4/4（与 R4-02 合流后） |
| R4-02 P2 | 旧无账号 scope 不再获得账号：返回身份不变的对象，报告保持 unknown/非 complete | `-k r4_02` 1 failed → 1 passed |
| R4-03 P2（工具） | `verify/run_round.py` 传播退出码、记录 command/HEAD/returncode/XML 完整 SHA/具名预期失败，缺 XML、解析失败、额外失败、测试数减少都非零 | 红：2 条非预期失败 → exit 1；绿：仅具名失败 → exit 0 |
| R4-04 P3 | `source_sidecars_created` 文本改为「本次运行期间出现，因果归属未完全证明，未删除」；字段保留、manifest 不升版本、不删旁车 | 3 条用例 + 51 项回归全绿 |

## 用户可见的变化

1. `organizer.group_scope.ensure_current`：旧 scope 没有账号、新快照有账号时，**返回对象的账号仍为空**（`account_id=""`、`account_id_state="not_recorded"`），不再被升级成新快照的账号；报告继续给 `account=unknown`、`complete=false`、CLI 退出 5。需要账号的调用方必须自己重新 `build_scope`。
2. `coverage` 报告的 `read.plan.copy` 新增 `attempts`（数字）与 `dropped_members`（名单）；重试后的 `members` 是**最终那次**复制的成员。
3. `maintain backup` 的提示文本：源库旁出现文件时不再说「本次读取让它新建」，改说「本次运行期间出现（因果归属未完全证明；无库内容；未删除）」。
4. 验证脚本（工具，不在产品代码里）：子测试失败不再被吞成成功退出。

## 验收

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

## 未做与遗留

1. 生产验收全部未做：真实库、真实账号写回、外置卷、云盘往返、真实备份恢复。
2. `maintain inspect/verify` 与 `ReadOnlyCrawlState` 的无参入口仍直接以 `mode=ro` 打开源库，会在 WAL 库旁留下同类文件；纳入统一读计划是下一批的事。
3. `samples.review` 仍会存下无指纹、消费时必然 unknown 的记录；`plans.make_plan` 仍不校验指纹。两者都要先决定规则来源。
4. V2 作品页期望内容代次仍未记录，交付维度只能 unknown。
5. `ensure_current` 返回空账号后没有可捕获的显式拒绝；将来接 CLI/续跑时若要硬失败，需检测 `account_id_state == "not_recorded"` 另加入口。
6. 私有副本的一致性仍只看名字与 `(size, mtime_ns)`；同尺寸改写并精确恢复 mtime 的 ABA 逃得过去。