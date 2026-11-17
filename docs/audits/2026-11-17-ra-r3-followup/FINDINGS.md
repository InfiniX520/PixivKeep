# R3 后续独立发现（7e657cb）

## R4-01 · P2 · 私有副本重试漏掉新 WAL，却声称 verified

### 位置

- `core/follow_facts_v2.py:491-571`：`_private_copy`
- `core/follow_facts_v2.py:508-510`：成员清单只在进入函数时计算一次
- `core/follow_facts_v2.py:525-566`：重试只更新 `before=after`，不更新 `members`，也不重建目标文件组
- `core/follow_facts_v2.py:693-699`：连接仍把该副本标为 `strict_no_side_effects=True`

### 确定时序

1. 源库是干净关闭的 WAL 库，初始只有主文件；第一笔提交为 1。
2. 第一趟复制主文件后，真实 SQLite 写者提交第二行 2；提交位于新出现的 `-wal`。
3. 第一次 after 检查看到新名字，进入第二趟，这是已有保护应发挥作用的时刻。
4. 第二趟仍只复制主文件；以新 before 比较后，名字和属性不再变化，于是返回 `verified=true`。
5. 私有副本查询只得 `[1]`，而源库已提交 `[1,2]`。

实测输出关键点：

```text
copy_calls = ['source.sqlite3', 'source.sqlite3']
copied_rows = [1]
copy.members = {'source.sqlite3': ...}
copy.verified = True
```

这不是已登记的 size/mtime ABA 限制：这里代码已经观察到了第一次的名字变化，问题在于**重试没有按新状态重建输入集合**。

### 影响

`read_only_connection` 的公开调用方可得到一份被标成已验证、实际漏掉已提交事务的副本。V2 与 legacy 覆盖报告、DesktopRuntime 都间接使用它。它不会写坏源库，但会让覆盖事实漏项或过期，因此是证据完整性问题，不应仅当文案缺陷。

### 反例

`test_r4_01_copy_retry_must_not_drop_committed_wal_rows`。钩子只负责在真实复制边界调度真实写者；复制、WAL、提交与查询均使用实际 SQLite/文件 IO，不是把返回值 mock 成期望。

## R4-02 · P2 · ensure_current 从无账号历史推导账号身份

### 位置

- `organizer/group_scope.py:623-678`：`ensure_current`
- `organizer/group_scope.py:647-650`：明确把旧无账号 scope 升级成新 scope 的账号
- `organizer/group_scope.py:667-673`：只有双方都有账号才比较；旧账号为空时不设防
- `tests/test_ra05_scope_account.py:311-320`：现有测试把这个推导固定成预期行为

### 反例

- 旧 scope：快照 `legacy-without-account`，无 `account_id`；
- 新快照：`later`，账号 B，成员集碰巧相同；
- batch：账号 B，但没有 snapshot_id（R3 明确允许这种未知形态）；
- 调 `ensure_current` 后，旧 scope 获得账号 B。

报告前后实测：

```text
before: account=unknown, complete=false,
        incomplete_reasons=['scope_account_unknown']
after:  account=matched, complete=true,
        incomplete_reasons=[]
```

成员集相同只能证明成员集相同，不能证明旧证据是从哪个账号取得的。相同 snapshot 标签也不是充分条件：RA-05 的原始反例正是“相同 snapshot 标签、不同账号”。

### 可达性与边界

GitNexus 和源码调用扫描显示，目前 `ensure_current` 的生产调用方尚未接入，直接调用方都是测试；因此**不能把它说成当前离线 CLI 已发生跨账号写回**。但它是公开 API，且返回对象可以直接进入 `build_report`，所以必须在将来接 UI/续跑前修正。

若 batch 绑定了旧 snapshot_id，后续 `build_report` 还可能因 snapshot 不同拒绝；这不是通用防线，batch 无 snapshot_id 的反例已证明可绕过。

## R4-03 · 验证门槛 · run_round.py 会把子测试失败留在成功进程里

`verify/run_round.py:45-63` 的 `run()` 返回 pytest returncode，但 `main` 在 99-124 行调用后不保存它，最终 138 行固定 `return 0`。

因此：

- `final2_adjacent.xml` 正确记录了 1 failed；
- 报告文字也正确写出 12/13；
- 但调用该 runner 的自动化仍会看到进程退出 0。

这不是“报告伪造通过”，而是**机器门槛不能独立阻止失败**。下一轮应同时按 returncode、XML 缺失、失败/错误数和允许失败名单判定，不可再靠人工阅读 prose 补救。

## R4-04 · P3 · 源旁车的“出现”与“由本 run 创建”要分开

`backup_library` 在 `family_before` 与 after 之间只比较名字存在性，字段叫 `source_sidecars_created`，CLI 写“本次读取让 SQLite … 新建”。若无并发，这个解释合理；但它不是排他所有权证明，其他进程也可能在同一窗口创建名字。

保留字段兼容即可，建议将解释改为：

> 本次运行期间出现；与本次 SQLite 读取的因果归属未完全证明；未删除。

不要借这条措辞修正去删除源旁车，也不要把 manifest v2 顺手升级。

## 已核对但未列为新缺陷的事项

1. **RA-04 两条旧反例互斥**：裁决成立，不应当为了全绿恢复未落库字典的信任。
2. **final2 是否包含后两修**：`final2_body.xml` 开始时间 11:22:51，晚于 `2aa18c3` 的 11:22:11；Tk/presentation 更晚。`RESULTS_R3.md` 页首的 `dfe2554` 是过期描述，不是最终全量漏跑的证据。
3. **跨 job pending delivery**：合成的合法外键链接仍为 `pending_verify`，未计成 saved，对照通过。
4. **同内容不同 delivery 折叠**：当前 R3 契约本来就要求同摘要折叠；这会减少 lineage 展示，但未证明会错算覆盖，不列实质缺陷。
5. **热 journal**：原实现夹具确为热 journal；`0xffffffff` 在现有 `>0` 判断下也会被判热，不再作为问题。
6. **探索过程中的无效探针**：未关闭初始 SQLite 连接、误用 `repo.db` 等早期试验均已排除；最终结论只引用收敛后的 `test_r4_findings.py` 与 `findings.xml`。
