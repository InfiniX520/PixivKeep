# 接线计划：先把引擎接成命令，再接进界面（2026-11-15）

起点 `bb4549d`（审计八条全修、复审无代码侧 P1、红基线 17/17、全量 3099 项）。
四个引擎目前**没有产品入口**：覆盖摘要、筛选解释/重评估、保存方案都只有库函数与测试。本节点只做接线，不改引擎语义。

## 0. 顺序与理由

1. **先 CLI**：确定性、可断言、不碰 Tk。用户能立刻用上，也为界面提供同一份输出形状。
2. **再界面**：界面线程只读同一份结果，不允许做全量哈希；Tk 测试按模块隔离进程跑。

## 1. CLI 契约（本轮交付）

新增两个子命令，**只读**，都支持 `--json`：

```text
maintain.py coverage --database <library.sqlite3> --run <run_id> [--json]
maintain.py filter-explain --candidates <file.jsonl> --conditions <file.json> [--json]
```

退出码（与既有维护命令一致的口径：结论不成立就不许 0）：

| 码 | coverage | filter-explain |
| --- | --- | --- |
| 0 | overall = complete | 每个候选都判成 match 或 exclude（没有任何 unknown） |
| 2 | overall = partial | 不用这个码 |
| 5 | overall = unknown | 有候选 verdict = unknown（判不了 ≠ 通过） |
| 3 | 输入不可读/参数被拒 | 输入不可读/参数被拒 |

判定细则：`coverage` 的 0 只在摘要 `overall == "complete"` 时给，`overall == "partial"` 给 2，`"unknown"` 给 5。`filter-explain` 只要有候选 `unknown` 就给 5，全部有确定结论才 0，输入坏给 3。**两个命令都不许"报告生成成功就给 0"**：0 必须等于"结论成立"。

## 2. 两个命令读到的东西

- `coverage`：`FollowCrawlStateRepository`（只读打开）→ `StateSource` → `gather_facts` → `build_coverage` → `text()`。**不写库、不推进游标、不改断点**。数据库以只读方式打开；拿不到就是退出码 3，不许当"空运行"。
- `filter-explain`：读候选 JSONL（每行一个候选对象，键同 `ArtworkMetadata`）与条件 JSON（`min_bookmarks`/`exclude_ai`），逐行 `explain()`，输出每行的 `verdict` 与 `reasons`（含 code/field/evidence/unknown）。**不发 HTTP、不读图库**。

## 3. 界面（第二步，本轮只冻结形状）

- 只读面板：显示 `coverage` 的 §4 文案 + 三维状态 + 未知项清单；不提供"重算/清理"按钮。
- 界面线程只调用已经算好的结果（主线程算完再投递），不在 Tk 回调里做哈希或解码。
- 每个 Tk 模块单独一个进程跑测试；沿用既有 `test_collection_*` 的隔离方式。

## 4. 文件归属

| 工作包 | 独占文件 |
| --- | --- |
| 接线 CLI | `core/report_commands.py`（新建，两个命令的实现）、`core/library_maintenance.py` 的 argparse 与 dispatch 两处、`tests/test_report_commands.py`（新建） |
| 界面 | 后续单列，先不分配 |

不许改引擎模块（`follow_*`、`filter_*`、`quality_*`、`png_decode`）与任何既有测试；发现引擎缺字段，报给集成者，不要就地改。

## 5. 完成定义

1. 两个命令在临时合成库/合成候选上跑通，`--json` 与文本两条出口都有测试；
2. 退出码表逐行有测试（0/2/3/5 各自的触发条件）；
3. 只读性有 AST 或运行期断言（不出现 `execute` 写语句、不出现 `commit`、不出现网络模块）；
4. 全量 `tests/` 不回归；红基线 17 条仍全绿。
