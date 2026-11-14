# U2-A 关注历史覆盖摘要：口径与示例

对象：一次关注历史遍历（一个 `FollowCrawlRun`）的**只读**摘要。它回答三件互不相同的事——
**查找走到哪**、**筛选判没判**、**保存核验齐没齐**——并且拒绝把"没测量"写成"零"。

契约与五路拆分见 [U2A_FIVE_WAY.md](U2A_FIVE_WAY.md)（含合流时的七处口径修订）。本文件只讲**怎么读这份摘要**。

## 1. 模块分工

| 模块 | 职责 | 是否读 I/O |
| --- | --- | --- |
| `core/follow_facts.py` | 从鸭子类型 source 抄出 run / artists / incomplete / budget | 只调用对方 getter |
| `core/follow_coverage_state.py` | 判三维状态（enumeration / decision / delivery） | 否 |
| `core/follow_coverage_counts.py` | 11 个计数、两个分母、恒等式自检 | 否 |
| `core/follow_coverage_text.py` | 把摘要渲染成中文句子 | 否 |
| `core/follow_coverage.py` | 组装成不可变 `FollowCoverage`，算 `overall` | 否 |

## 2. overall 怎么来

`overall` 只有三个值，含义严格：

| overall | 条件 |
| --- | --- |
| `complete` | 三维全部成立（查找 complete、判定 applied、保存 complete）**且**计数自洽 |
| `partial` | 至少一维**明确**没完成（查找 partial/stopped/failed、判定 unknown_fields、保存 partial/missing） |
| `unknown` | 没有任何一维明确没完成，但有维度**证明不了**（保存 unknown、判定 skipped、或计数不自洽） |

"没做到"和"没证明"是两件事，所以 overall 不是布尔。

## 3. 计数与分母

- 11 个计数：画师级 5 个（由 `artists` 列表直接统计）+ 运行级 6 个（只认 `run.totals`）。
- **缺失写 null**，不写 0。文本遇到 null 就整句不说。
- 唯一允许推导的是 `artworks_seen`：仅当 `totals["total_artists"] == len(artists)` 且每个 `artist["pages"]` 都是非负整数时，取 `sum(pages)`，并把 `artworks_seen_from_artist_sum` 记进 `reasons`（只标来源，不改自洽性）。
- 分母（`artists_total` / `artworks_total`）只认运行级显式声明；**永不**用列表长度或求和值顶替。分母为 null 时文本不打印"共 N 件"这类句子。
- `consistent=False` 时文本会点名不自洽的原因，并强制走未完成结论。

## 4. 文本规则

1. 三维各一句：`查找:` / `判定:` / `保存:`，各自带状态与原因码。
2. 计数为 null 的分句不出现；运行级计数全 null 时只输出一句"没有来源，无法给出"，句内**不含任何数字**。
3. `已查完 / 已穷尽 / 全部完成` 只在 overall=complete 的完成句里出现；未完成分支连否定式都不含这三个词，所以 grep 到它们就一定是真的完成。
4. 结论句只列**真的没成立**的维度：`结论: 以下未成立——筛选判定未完成或未应用；这些数字不能当作覆盖结论。`
5. 示例数字必须带"（示例）"；`build_coverage` 永不置 `sample`。

## 5. 三段示例输出（**合成数据**，fixtures 见 `tests/follow_fixtures.py`）

这三段是 `build_coverage` 在合成夹具上的真实输出，**不含**行内标记；文档里要展示数字时，
调用方把 `sample` 置为 `True` 再由 `render()` 追加「（示例）」——下面是带标记的版本，
标记只出现在含数字的那一句上（没有数字的句子不加标记，这也是测试钉住的）。

```text
# 全部完成、计数齐全
本轮已检查 3 位画师。
确认当前范围处理完 3 位；0 位待继续，0 位有失败页，0 位覆盖待核对。
新增 34 个作品、保存 19 页；已有 0 页通过完整核验；筛选排除 15 件，字段未知 0 件。（示例）
范围内画师总数 3 位（有记录可对）。
查找: 来源遍历完成
判定: 筛选判定已应用
保存: 保存与核验已齐
结论: 本轮范围已查完，保存已齐。

# 预算停止
本轮已检查 3 位画师。
确认当前范围处理完 1 位；2 位待继续，0 位有失败页，0 位覆盖待核对。
新增 12 个作品、保存 12 页；已有 0 页通过完整核验；筛选排除 0 件，字段未知 0 件。（示例）
范围内画师总数 3 位（有记录可对）。
查找: 因预算或限流停止（budget）
判定: 筛选判定已应用
保存: 部分页面未保存或未核验（artist_pending:1002）
停止原因: 本批请求预算用完（budget）
结论: 以下未成立——来源遍历未完成、保存未齐；这些数字不能当作覆盖结论。

# 真实运行的样子（持久化的只有 total_artists）
本轮已检查 3 位画师。
确认当前范围处理完 3 位；0 位待继续，0 位有失败页，0 位覆盖待核对。
新增 34 个作品。（示例）
范围内画师总数 3 位（有记录可对）。
查找: 来源遍历完成
判定: 筛选被跳过（no_filter_facts）
保存: 保存与核验未得证明（saving_not_measured）
结论: 以下未成立——筛选判定未完成或未应用、保存与核验未得证明；这些数字不能当作覆盖结论。
```

第三段是关键：**它没有撒谎**。保存侧没有任何持久化证据时，摘要说"未得证明"，而不是"保存齐了"。

另外注意第一段与第三段的差别只在"计数有没有来源"：同样的运行，缺 `pages_saved` 就少了
"保存 19 页……"那半句，而不是把它写成 0。

## 6. 已知限制（现状，不是待修 bug）

1. **只读状态库做不出"保存已齐"。** `crawl_runs` / `artist_progress` 里没有 pages_saved / excluded_by_filter 的持久化，所以真实运行的 `delivery` 只能是 `unknown`。要让真实运行报 complete，需要在运行结束时把这三个保存侧计数落库（后续节点）。
2. **真实运行的 `decision` 多半是 `skipped`。** 筛选计数同样没落库；要报 applied，必须有现场来源（例如当次的 `SourceRunReport`）或后续持久化。
3. **`page_end_evidence` 只到"清扫证据"粒度。** 它由 `artist_progress.swept_at` 推导：全部有值=complete、部分=partial、都没有=unknown。它证明的是"这些画师本次被走到"，不是"该画师作品历史被读到底"。
4. **id 空间不同。** 真实仓库的 `incomplete_artists(job_id)` 与 `source_budget_status(batch_id)` 按 job/batch 取，不是 run_id；`StateSource` 的 `incomplete` / `budget` 参数由调用方按正确 id 空间提供，本模块不猜。
5. 摘要本身不设预算、不改游标；它只描述既有事实。

## 7. 用法

```python
from core.follow_facts import StateSource, gather_facts
from core.follow_coverage import build_coverage

source = StateSource(repository, run_id)          # 有 job_id 视角的调用方再传 incomplete=/budget=
facts = gather_facts(source)
coverage = build_coverage(
    facts,
    scope={"kind": "follow_run", "run_id": run_id, "account_ref": run.account_id},
)
print(coverage.text())          # 人类摘要
payload = coverage.as_dict()    # JSON 安全，可直接落盘/给 GUI
```

本轮**不接 GUI、不加 CLI 子命令**：GUI 接线在 U2-A 与 U2-B 都合流之后统一做，避免两条 lane 抢同一批视图文件。
