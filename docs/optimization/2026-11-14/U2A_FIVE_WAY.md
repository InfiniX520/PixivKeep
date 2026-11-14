# U2-A 五路拆分（关注历史的覆盖与更新）

基线：U1-C 收口后（提交 `2aa3a05`）。**只读**：不改断点、不推进游标、不写库；只把既有事实收成一个带范围的摘要。

## 0. 既有事实（我已核对过的入口）

| 来源 | 能给出 |
| --- | --- |
| `core/follow_crawl_state.py` `FollowCrawlRun` / `ArtistProgress` / `list_artists` | 一次遍历的状态、stop_reason、每个画师的 state 与计数 |
| `core/follow_crawl_state.py` `refresh_run_totals` | 运行级 totals（只读调用，不改变语义） |
| `core/follow_repository.py` `incomplete_artists` / `source_budget_status` | 未完成的画师（带原因）、预算状态 |
| `core/follow_collection.py` `SourceRunReport` | 一次采集的 coverage/reason（页面级事实） |

**这些模块本轮都不改**；A1 只读它们的公开方法（用鸭子类型的输入，便于测试注入假对象）。

## 1. 冻结契约

```text
A1 core/follow_facts.py
   gather_facts(run: object) -> {
     "run":      {run_id, account_ref, config_key, status, stop_reason, totals},
     "artists":  [{artist_id, state, pages, newest_illust_id}],       # 按 artist_id 排序
     "incomplete": [{artist_id, reason}],                             # 只读列表，不改状态
     "budget":   {state, remaining} | None
   }
   只调用给定对象上的公开方法/属性；读不到就抛 TypeError（不猜）。

A2 core/follow_coverage_state.py
   coverage_states(facts) -> {
     "enumeration": {state, reason},   # complete | partial | stopped | failed
     "decision":    {state, reason},   # applied | unknown_fields | skipped
     "delivery":    {state, reason}    # complete | partial | missing
   }
   规则（每条都要有测试）：
   - 任一画师 state 表示失败/未完成 → enumeration=partial（reason 带首个原因），不能报 complete；
   - run.status 是 paused 且 stop_reason 是预算/限流 → enumeration=stopped；
   - 三维互相独立：decision 说"筛选判定"，delivery 说"保存/核验"，不得用一个布尔合并；
   - 三者都是 complete 才允许 overall=complete（overall 由主线程的组合器算，不在本模块）。

A3 core/follow_coverage_counts.py
   coverage_counts(facts) -> {
     "counts": {artists_checked, artists_confirmed, artists_pending, artists_failed_pages,
                artists_coverage_unverified, artworks_seen, pages_saved,
                pages_already_verified, excluded_by_filter, unknown_fields},
     "denominators": {artists_total: int|null, artworks_total: int|null},
     "consistent": bool     # counts 内部恒等式是否成立（例如 confirmed+pending+failed+unverified == checked）
   }
   未知就写 null，**不得**用估算值冒充分母；恒等式不成立时 consistent=False 并给出 reason 列表。

A4 core/follow_coverage_text.py
   render(coverage: Mapping) -> list[str]
   输出设计 §4.1 那种摘要（"本轮已检查 N 位画师。确认处理完 X 位；Y 位待继续……"），
   并且：分母为 null 时**不许**打印"共 N 件"；示例数字必须带"（示例）"标注；三种状态各有一句。

A5 tests/test_follow_coverage_matrix.py
   只用合成 facts 字典（不建库）覆盖：全部完成 / 部分画师失败页 / 预算停止 / 限流暂停 /
   重复分页 / 首屏全被过滤 / 私密范围缺失 / 空关注列表；每条断言三维状态、counts 恒等式、
   以及"未完成时文本里不出现'已查完'"。
```

## 1.1 合流时对齐的口径（2026-11-14，修订记录；上面的契约原文保留不改）

五路各自实现完成后，主线程按"不知道不许写成知道"逐条复核，改了下面七处。每条都改了代码与测试，不是文档口径。

1. **A1 的入参是 source，不是 run。** `gather_facts(source)` 里的 source 是鸭子类型（`run` 属性/可调用 + `artists(run_id)` + 可选 `incomplete_artists` / `source_budget_status`）；真实仓库用 `StateSource(repository, run_id, incomplete=…, budget=…)` 包一层。理由：`FollowCrawlRun` 行本身不含 artists/incomplete/budget。
2. **artist["pages"] 是"该画师本次看到的作品数"**（来自 `artist_progress.works_seen`），**不是**保存的页数。A5 的夹具最初按"保存页数"造数，导致"可证明求和"会算出错误的作品数，已按本条改正并加了一条"seen ≠ saved"的守护测试。
3. **artist["swept_at"] 透传**（增量清扫完成时间，空串=本次没有完成证据）。它只用于 `page_end_evidence`，不参与任何计数。
4. **A3 的计数是 11 个**：画师级 5（checked/confirmed/pending/failed_pages/coverage_unverified）+ 运行级 6（artworks_seen/pages_saved/pages_already_verified/excluded_by_filter/unknown_fields/duplicate_pages）。原契约的 `artworks_seen` 是**报告词表**，源键是 `totals["works_seen"]`，两者故意不同名。
5. **运行级计数只认 `facts["run"]["totals"]`，缺失即 null**，唯一例外是 `artworks_seen` 的"可证明求和"：仅当 `totals["total_artists"] == len(artists)` 且每个 `artist["pages"]` 都是非负 int 时，才允许 `sum(pages)` 并把来源标成 `artworks_seen_from_artist_sum`。分母永不接受求和值，也永不把 `len(artists)` 提升为分母。
6. **A4 的渲染对 null 的反应是"整句不说"，不是"写 0"。** 计数为 null 时对应分句不出现；运行级计数全 null 时只输出一句"没有来源，无法给出"，句内不含任何数字。
7. **delivery 增加第四个状态 `unknown`**（保存/核验未得证明）。原因：真实 `FollowCrawlRun` 不持久化任何保存侧计数，所以"每个画师都 finished 且 run 完成"并不足以证明保存已齐；同时"本应保存 30 件、实际保存 0 件"这种明显缺失也必须被抓住。新判定顺序：given → 无画师(complete) → 有画师重试(partial) → run 未完成(partial) → 有画师未 finished(partial) → **保存侧计数缺失(unknown/saving_not_measured)** → **应保存数 > 已保存数(missing 或 partial/pages_not_saved:N)** → complete。
   连带：`overall` 的三态是 `complete / partial / unknown`，`delivery=unknown` 时 `overall` 必须是 `unknown`（不是 partial）——"没证明"不等于"没做到"。

### 由此得到的两条产品事实（不是缺陷遮掩，是现状）

- **只读状态库做不出"已保存齐"的结论。** 没有 pages_saved/excluded_by_filter 的持久化，摘要最多到 `overall=unknown`，文本会明确写"保存与核验未得证明"。要让真实运行能报 complete，需要在运行结束时把这几项计数落库（属后续节点，不在 U2-A 范围）。
- **真实运行的 decision 多半是 `skipped`。** 筛选计数同样没有落库，所以"筛选判定已应用"必须有外部来源（现场 `SourceRunReport` 或后续持久化），否则摘要会如实说"筛选被跳过"，而不是假装算过。

## 2. 主线程合流后做

- `core/follow_coverage.py`：`FollowCoverage` 冻结 dataclass（scope/enumeration/decision/delivery/counts/denominators/stop_reason/page_end_evidence/notes）+ `build_coverage(facts, *, scope)` + `as_dict()`；
- 用 A1–A4 组装，并计算 `overall`（三者皆 complete 且 consistent 才是 complete）；
- CLI/文档：本轮只给库函数与一段 `python -c` 示例，**不接 GUI**；GUI 接线在 U2-B 之后统一做。

## 3. 完成定义

五路测试全绿；合流后 `build_coverage` 在合成 facts 上通过 A5 的全部场景；文本不含未证明的分母；全量矩阵与三个 Tk 模块不回归。
