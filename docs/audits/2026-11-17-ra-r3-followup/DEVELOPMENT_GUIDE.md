# R4 精确开发指导（从 7e657cb 之后接续）

## 0. 开工边界

1. 从`7e657cbb8ae1092f5c8aa93f15f2ec0925ac0ec7`或其后续**已审修复提交**建独立 `fix/ra-r4-20261117`，不要从日常 `4ba891e` 开修。
2. 保留旧 `docs/audits/**`、旧 `test_boundaries.py`、旧 `test_adjacent.py` 与 R3 文档原文。本轮报告另存，不回写历史结论。
3. 本轮只修 R4-01、R4-02、验证门槛和明确的文案问题；不顺手做 V2 迁移、归档上传、真实恢复或账号写回。
4. 先用本报告两项红测试在 `7e657cb` 复现 **2 红 / 2 对照绿、0 设置错误**，再改实现。修后同一文件必须 4/4。
5. 若之后派发子智能体，主智能体先完成核心文件、接口、数据流和文件归属对齐并向用户结束该轮；用户确认后再用 `subagent_fork`。同一文件/事务不并行改，并原样附上工作区要求的完整质量提示。
6. 不为修这两个问题重写六包，不降低原断言，不删除旧反例。

## R4-A：修复 WAL 私有副本重试

**独占文件**：`core/follow_facts_v2.py` 的 `_private_copy` 及必要调用点；新增/扩充 `tests/test_ra02_read_plan.py`。  
**调用方回归**：`core/report_commands.py`、`core/collection_desktop.py` 只读整链，尽量不改它们。

### 必须先冻结的不变量

- 每次 attempt 的成员清单必须来自**该次 attempt 的最新源状态**；不得复用上一趟的 `members`。
- 第二趟发现 WAL 后，副本必须含主文件与该 WAL；`copy.members` 必须如实列出二者。
- 第一次目标里可能留下的文件不得混进第二次证据。最简单、最不易出错的做法是每次 attempt 使用独立子目录，整次失败只清自己的根；不要在原目标上“凑着覆盖”。
- 若复制期间源继续动，达到尝试上限就 `source_changed_during_copy`，不得返回数字。不要为了成功而把私有副本模式中途改成活库直读。
- `copy.verified` 只有在当前 attempt 的必需成员复制完整、名字与稳定性检查通过后才可为 true。
- 仍不复制 `-shm`，仍不对源执行 checkpoint、rollback、DDL 或删除。
- 源目录严格无副作用的承诺仍只覆盖已有证据窗口：复制前后及直接读取的实际观察区间；不得扩写成连接整个生命周期都能看见源目录变化。失败和取消都要清理自己的副本。

### 推荐实现顺序

1. 把反例移成自包含的产品测试，不依赖 `.runtime` 或另一个测试模块的私有 helper。
2. 把“建立一次 before”与“执行一次 attempt”分开；每次 attempt 内部重新确定主文件/WAL 集合。
3. 重试时更新 before、目的目录、digests、source_members 和 difference，不只更新 before。
4. 若用 attempt 子目录，返回的连接持有最终目录，关闭时从本次私有根整体清理；不要按任意外部路径递归删。
5. 报告里的 `read_mode/copy/members/verified` 从最终 attempt 产生，不能保留第一趟的空 members。

### 必测矩阵

- 初始无 WAL → 第一趟后出现 WAL 与提交（本反例）；
- 初始有 WAL → 第一趟复制中 WAL 追加/换名；
- 第二趟仍变化 → 类型化拒绝，不返回摘要；
- 重试前旧目标有残留 WAL，新 attempt 不需要它 → 不得误读或误报 verified；
- 干净关闭 WAL、WAL 无 SHM、活 WAL 三行原选择表全部保留；
- 取消、复制异常、连接打开异常、close 后无临时残留；
- legacy CLI、V2 CLI、DesktopRuntime 至少各有一条经过修复路径的端到端；
- 前后源文件族名字、摘要和属性符合所声明 promise，不能用“数据库没变”掩盖新增旁车。

## R4-B：禁止 ensure_current 隐式补账号

**独占文件**：`organizer/group_scope.py`、`tests/test_ra05_scope_account.py`；必要时只补报告测试，不改 report 的已正确账号比较。

### 推荐裁决：拒绝静默升级

当旧 `scope.account_id == ""`、新快照有账号时，抛出明确的 `scope_account_unproven`（或新稳定码 `scope_account_requires_rebind`），要求调用方重新 `build_scope` 并留下明确的重新绑定动作。

理由：即使 snapshot_id 相同，也没有证明“以前缺账号的导出”和“现在带账号的导出”属于同一账号；成员集相同更不构成身份链。当前没有生产调用方，正适合在接线前收紧。

若产品坚持返回可继续使用的对象，则返回对象的账号仍必须为空并继续报告 unknown；不能返回 `fresh` 让账号自动变成 B。但这种方式字段语义更复杂，**不推荐**。

### 必测矩阵

- 已知同账号 + 同成员：通过；
- 已知不同账号：`scope_account_changed`；
- 已知账号 → 新快照无账号：拒绝；
- 无账号 → 新快照有账号：按新契约拒绝或保持 unknown，绝不能自动 matched；
- 双未知：保持 unknown；
- 同 snapshot 标签、同成员、不同账号：不能借标签通过；
- batch 无 snapshot_id：经 `ensure_current` 后仍不得把 `proof_limits.complete` 从 false 变 true；
- 旧 scope 直接 `scope-plan/batch-report` 的退出 5 行为不变；
- 序列化/反序列化后仍不能补出账号。

需要修改现有 `test_ensure_current_upgrades_a_legacy_scope_instead_of_stranding_it`：它是把错误推导当功能的测试，不是必须保留的旧冻结反例。修改时在 R4 契约中列出旧断言、替换断言与理由。

## R4-C：让验证脚本真正能失败

不要覆盖 `final2_*`。以新 prefix 和新结果目录运行。

1. 保存每组的 command、工作树、HEAD、开始/结束时间、returncode、XML 路径和完整 SHA-256。
2. 缺 XML、XML 不可解析、pytest 非零、非预期 failed/errors、测试数变化都要进入机器结果；主进程不得无条件 0。
3. **历史 13 条与当前验收分开**：
   - 历史文件原样运行，预期仍是 12 通过 + 1 条具名过期前提失败；
   - 当前集合可 deselect 这一具名用例，但必须同时运行 canonical review 替代模块；
   - 不允许只加 `--ignore`/`--deselect` 后写“13/13”；
   - 不允许修改历史文件来追绿。
4. `targeted.xml` 是主体子集，不重复累加；旧 frozen 与 adjacent 也分别列。
5. 在结果 JSON 中明确 `expected_failures` 的完整 nodeid、实际失败 nodeid 和原因。出现额外失败必须退出非 0。
6. 提交落地后按新 HEAD 重跑；代码/测试提交时间、XML 时间与工作树状态一起留证。

## 文档小修

- `RESULTS_R3.md` 是历史文件，不在 R4 偷改；在新的 R4 结果里注明 `dfe2554` 页首记录已过期，最终 XML 时序支持后两修已包含。
- 源旁车文案改成“运行期间出现/归因未证明”，保留 `source_sidecars_created` 字段以兼容现有 manifest。
- 更新当前导航和 `TASK_PLAN.json` 的 `active_workstream`；旧 `fixed_commit=null`、`RA-01..06 open` 不应继续当最新状态。
- R4 closure 必须保留两项新红→绿、旧 13 条的 1 红裁决、全部 XML 计数和生产未做五栏，不写成“已上线”。

## R4 验收顺序

1. 两项新反例与对应模块；
2. 备份、读计划、legacy read、覆盖、organizer、质量的定向组合；
3. 主体、Tk 四模块、presentation 两模块；
4. 旧 15 frozen、旧 13 历史相邻（具名 1 红）、canonical 替代用例、新 4 条；
5. `compileall core gui organizer utils maintain.py`；
6. 工作树/冻结文件 diff、完整 XML 哈希、导航与维护记录；
7. 主智能体先检查全部测试，再用继承上下文的只读 fork 对契约逐行审 diff；失败退回原开发者，不新开空白实现代理。

## 之后再排期的独立工作

这些**不混入 R4 小修**：

1. 把 `maintain inspect/verify` 与 `ReadOnlyCrawlState` 的无参入口纳入统一读计划，并检查 legacy 报告是否拒绝名字变化窗口；备份的在线 API 不能机械换成静态副本。
2. 先决定审阅/计划命令的 canonical RuleSet 来源，再让 `samples.review` 拒绝无指纹新记录、`plans.make_plan` 校验指纹并把规则身份带入 plan；旧记录保持 unknown，不改写历史。
3. V2 作品页期望内容代次是数据面设计：先定来源、事务、代次/重试语义、旧数据 unknown、备份回退和迁移测试，再做 DDL。禁止把 observed SHA 填进 expected。
4. 外部 WAL/SHM 回收、真实恢复、外置卷/云盘、真实账号写回与生产部署全部另批，未获明确授权不做。
