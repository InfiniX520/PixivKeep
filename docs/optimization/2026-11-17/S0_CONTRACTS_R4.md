# S0 契约（第四轮，R4）：两项边界修复与验证门槛

锚点：实现 `7e657cb`，审计 `3f5f141`（分支 `audit/ra-r3-followup-20261117`，未推送）。本分支 `fix/ra-r4-20261117` 从审计提交建。
反例文件：`docs/audits/2026-11-17-ra-r3-followup/evidence/test_r4_findings.py`
（sha256 `d8eae7ecad04c02df438535dbf575b2379715b80dceee4a1edd3e5b3882853e8`），逐字节不改；复本在 `.runtime/jpgpng_ra_r4_fix_20261117_01/verify/` 用于运行。

## 0. 本轮边界

1. 只修 R4-01（WAL 私有副本重试）、R4-02（`ensure_current` 不得补账号）、R4-03（验证脚本不能永远退出 0）与明确的文案问题。
2. 不动 schema、不做迁移、不做真实恢复/释放、不做账号写回、不部署、不装解码器。
3. 冻结：`docs/audits/**`、`tests/test_boundaries.py`、`test_adjacent.py`、以及历史 `RESULTS_R3.md`（不回写；过期描述在新报告里说明）。
4. 不重写六包、不降低原断言、不删除任何旧反例。每项都要「修前红 → 修后绿」，同一反例文件修后必须 **4/4**。

## 0.1 修前复现（已在 3f5f141 上完成）

```
PIXIVKEEP_AUDIT_REPO=<worktree> pytest verify/test_r4_findings.py -q
--> 2 failed / 2 passed / 0 error, 2.28 s      verify/baseline_r4.xml
    R4-01: copy_calls=['source.sqlite3','source.sqlite3'], members 只有主文件, verified=True, copied_rows=[1]（源库已有 [1,2]）
    R4-02: upgraded.account_id='account-B'，报告 proof_limits 从 unknown/complete=false 变成 matched/complete=true
```

## 1. R4-A：私有副本重试必须按新状态重建

**产品变化**：`_private_copy` 的每个 attempt 都是一次独立、自洽的复制；重试不再沿用上一趟的成员清单与目标目录。

**不变量（缺一不可）**

1. 每次 attempt 的成员清单（主文件、存在的 `-wal`）来自**该 attempt 开始时**的源状态；不得复用上一趟的 `members`。
2. 每次 attempt 使用独立子目录（如 `<私有根>/attempt-<n>/`）；清理只针对本次私有根，不按外部路径递归删。
3. 重试必须同步更新：`before`、目标目录、`members`、`source_members`、`difference`、最终 `plan.copy`。报告的 `read_mode/copy/members/verified` 一律取最终 attempt。
4. 达到尝试上限 → `source_changed_during_copy`（非零、不返回任何数字）。
5. `copy.verified=true` 仅当**当前 attempt** 的必需成员（主文件 + 存在时的 `-wal`）复制完整、名字与稳定性检查通过。
6. 仍不复制 `-shm`；仍不对源执行 checkpoint、rollback、DDL、删除。
7. 不得为了成功把私有副本中途改成活库直读；不得放宽已声明的 promise。
8. 失败与取消都要清理自己的副本目录（关闭路径 + 异常路径）。
9. 源目录零副作用的承诺只覆盖**实际观察窗口**（复制前后 / 直读期间），不得扩写成整个连接生命周期。

**公开表面不变**：`read_only_connection(path, *, immutable_source, allow_private_copy, private_root, stop)` 签名与返回对象；
`plan.copy` 既有字段名（`dir`/`members`/`verified`/`source_members`/`reason`）保持。
**允许新增**：`plan.copy["attempts"]`（int）、`plan.copy["dropped_members"]`（上一 attempt 有、当前没有的名字）——给证据用，不得用来掩盖失败。

**必测矩阵**
- 初始无 WAL → 第一趟后出现 WAL 与提交（本反例，必须绿）；
- 初始有 WAL → 第一趟复制中 WAL 追加/换名；
- 第二趟仍变化 → 类型化拒绝，不返回摘要；
- 上一 attempt 的目标里有残留 `-wal`、新 attempt 不需要它 → 不得误读、不得误报 verified；
- 干净关闭 WAL / WAL 无 SHM / 活 WAL 三行原选择表全部保留（`tests/test_ra02_read_plan.py` 不许变红）；
- 取消、复制异常、连接打开异常、close 后无临时残留；
- legacy CLI、V2 CLI、DesktopRuntime 至少各一条经过修复路径的端到端；
- 前后源文件族名字/摘要/属性符合所声明的 promise。

## 2. R4-B：`ensure_current` 不得推导账号

**裁决：拒绝静默升级。** 旧 `scope.account_id == ""`、新快照有账号时，抛新稳定码
`scope_account_requires_rebind`，要求调用方重新 `build_scope`；不得返回把旧 scope 升成新账号的对象。

**四态表**

| 旧 scope | 新快照 | 结果 |
| --- | --- | --- |
| 有账号，相同 | 有账号 | 放行（成员集指纹仍须相同） |
| 有账号，不同 | 有账号 | `scope_account_changed` |
| 有账号 | 无账号 | `scope_account_unproven`（保持现状） |
| 无账号 | 有账号 | **`scope_account_requires_rebind`（本轮新行为）** |
| 无账号 | 无账号 | 放行，返回对象账号仍为空 |

理由：成员集相同只证明成员集相同；相同 snapshot 标签也不构成身份链（RA-05 原始反例正是同标签不同账号）。
当前没有生产调用方，正适合在接线前收紧。

**公开表面**：`ensure_current(scope, snapshot, *, groups=None)` 签名不变；新增错误码；`GroupScope` 字段不变。

**现有断言改写（必须做，且要在报告里贴 diff）**

`tests/test_ra05_scope_account.py::test_ensure_current_upgrades_a_legacy_scope_instead_of_stranding_it` 第 311-320 行
把「旧无账号 → 新快照有账号 → 升级成账号」写成预期行为，正是本项要否定的语义。改为：

```
def test_ensure_current_refuses_to_invent_an_account_for_a_legacy_scope():
    # A dump written before the account was exported cannot name one.  A later
    # snapshot naming an account does not prove the old evidence came from that
    # account: the same members and the same label are not an identity chain.
    before = scope_of(account=None)
    assert before.account_id == ""
    assert code_of(lambda: ensure_current(before, snapshot())) == "scope_account_requires_rebind"
```

这是**唯一**允许改写的既有断言；其余（同账号放行、移动账号拒绝、快照失账号拒绝、双未知放行）一条不动。

**必测矩阵**
- 已知同账号 + 同成员：通过；
- 已知不同账号：`scope_account_changed`；
- 有账号 → 快照无账号：`scope_account_unproven`；
- 无账号 → 快照有账号：`scope_account_requires_rebind`（不能自动 matched）；
- 双未知：放行且账号仍为空；
- 同 snapshot 标签、同成员、不同账号：不能借标签通过；
- batch 无 snapshot_id：经 `ensure_current` 后 `proof_limits.complete` 不得从 false 变 true；
- 旧 scope 直接 `scope-plan/batch-report` 的退出 5 行为不变；
- 序列化/反序列化后仍不能补出账号。

## 3. R4-C：验证门槛必须能失败

`verify/run_round.py`（工具，不入产品代码）要求：

1. 每组保存：command、worktree、HEAD、开始/结束时间、pytest returncode、XML 路径、XML 完整 SHA-256、用例数四元组。
2. 机器判定：任一组 returncode 非零（除具名预期失败）、XML 缺失、XML 不可解析、failed/errors 超出预期名单、测试数变化 → 主进程 **非零**。
3. 具名预期失败：`expected_failures` 里写完整 nodeid + 原因；实际失败集合必须**等于**该名单（多了少了都非零）。
4. 历史 13 条与当前验收分开：历史文件原样运行（预期 12 通过 + 1 条具名失败）；另跑 `tests/test_ra04_canonical_review.py` 作为替代证据；不得只 `--ignore/--deselect` 后宣称 13/13。
5. 输出机器可读 `matrix_summary.json`（每组 command/HEAD/returncode/SHA/counts/expected/actual）。
6. 不覆盖 `final2_*`；用新前缀与新结果目录。

## 4. 文案收紧（P3）

`source_sidecars_created` 字段**保留**（manifest 不升版本），但文本与 docstring 改为：
「本次运行期间出现；与本次读取的因果归属未完全证明；未删除」。
不借这条措辞去删除源旁车。

## 5. 文档与导航

- 历史 `RESULTS_R3.md` 不回写；在 R4 结果里注明「页首 `dfe2554` 记录已过期，`final2_*` 的时间戳晚于后两修」。
- `TASK_PLAN.json` 的 `active_workstream`、`docs/optimization/IMPLEMENTATION.md` 当前段、`docs/README.md` 条目指向 R4；旧的「RA-01..06 open / fixed_commit=null」不得继续当最新状态。
- 新报告：`CLOSURE_R4.md`（五栏 + 两项红→绿 + 旧 13 条 1 红裁决 + 全部 XML 计数 + 生产未做）、`REVIEW_R4.md`（只读复审逐条）。

## 6. 文件所有权（并行边界）

| 角色 | 独占文件 |
| --- | --- |
| lane A（R4-A） | `core/follow_facts_v2.py`（`_private_copy` 及必要调用点）、`tests/test_ra02_read_plan.py`、新增 `tests/test_r4a_copy_retry.py` |
| lane B（R4-B） | `organizer/group_scope.py`、`tests/test_ra05_scope_account.py`、新增 `tests/test_r4b_rebind.py` |
| 集成人 | `verify/run_round.py`、`core/library_maintenance.py`（仅文案）、`organizer/report.py`（若必须）、docs、`TASK_PLAN.json` |

同一文件不得两个 lane 同时改。`core/report_commands.py`、`core/collection_desktop.py` 只读回归，尽量不动。

## 7. 验收顺序

1. 两项新反例与对应模块；
2. 备份、读计划、legacy read、覆盖、organizer、质量的定向组合；
3. 主体（排除 6 个 Tk 模块）、Tk 四模块、presentation 两模块；
4. 旧 15 frozen、旧 13 历史相邻（具名 1 红）、canonical 替代模块、新 4 条；
5. `compileall core gui organizer utils maintain.py`；
6. 工作树/冻结文件 diff、完整 XML 哈希、导航与维护记录；
7. 主智能体先跑全部测试，再用继承上下文的只读 fork 逐行审 diff；失败退回原开发者。

## 8. 本轮不做

schema 迁移、真实恢复/释放、外置卷/云盘、真实账号写回、生产部署、安装解码器、修改历史反例或历史报告、把 observed SHA 当 expected。