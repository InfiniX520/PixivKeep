# 覆盖摘要与筛选解释：界面接线方案（2026-11-15）

只写方案，不改代码。所有行号对 `c10919b` 固定。本方案先读过 `gui/collection_view.py`、`gui/collection_controller.py`、`gui/verification_panel.py`、`gui/collection_sources.py`、`gui/collection_app.py`、`gui/collection_dialogs.py`、`core/collection_desktop.py`、`core/follow_coverage*.py`、`core/filter_explain.py` 与相关测试；没有起 Tk 窗口做交互实验。

## 0. 结论先说

两处接线，风险完全不同：

| 接线 | 落点 | 形态 | 风险 |
| --- | --- | --- | --- |
| A 覆盖摘要 | **新页面 `coverage`** + 新面板 `CoveragePanel` | 只读报告，worker 读一次仓库，Tk 只显示 | 中（要新增只读读取器；Tk 有既有 flake） |
| B 筛选解释 | **记录页已有的"作品详情"**，不新建页面、不新增控件 | 纯函数：把记录行里已有的 `reason_codes` 翻译成人话 | 低（无 I/O、无 worker、无 Tk 新结构） |

**B 不需要新数据源**：记录行已经带着 `reason_codes`（`core/library_queries.py:822`，值来自 `batch_items.reason_codes_json`，查询列见 `:139`），详情文本就是 `gui/collection_controller.py:714` 拼的那一句。所以"为什么没下载"可以直接在详情里补一段，而不是另开一个解释面板。

## 1. 挂点

### A. 覆盖摘要 → 新页面 `coverage`

要改的地方（只有三处 + 一个新文件）：

1. `gui/collection_view.py:26` `PAGE_KEYS` 插入 `coverage`：`("follow","tasks","sources","records","verify","coverage","settings")` —— 放在 `verify` 之后、`settings` 之前，把两个"诊断/证据"页面放一起。
2. `gui/collection_view.py:27` `PAGE_LABELS` 加 `coverage` 的中文（建议「覆盖」）。
3. `gui/collection_view.py:356-366`（`_build_content` 的循环）加一行 `self._build_coverage_page(self._pages["coverage"])`，并新增 `_build_coverage_page`，形状比照 `_build_verify_page`（`:786-804`）：一个 heading + 一个面板，面板自己拥有内容与文案。
4. 新文件 `gui/coverage_panel.py`（见 §3）。

**不改动的东西**（这是边界）：`_build_follow_page`(:527)、`_build_records_page`(:734)、`_build_tasks_page`(:673)、`_build_sources_page`(:719)、`_build_settings_page`(:808) 一行都不改；下载表单、筛选条、任务列表、日志面板一律不碰。导航栏本身也不用改代码：它是按 `PAGE_KEYS` 循环生成的（`:324-333`），加 key 自动多一个按钮，窄屏重排逻辑（`_update_header_layout` :338-346）已经是按数量自适应的。

**为什么是独立页而不是塞进关注页**：关注页已经是"三种模式 + 表单 + 筛选"的滚动页（:527-541），覆盖摘要是**只读证据**，塞进去会让"操作"和"结论"混在同一屏，而且用户看覆盖的时机（"上次到底查到哪"）和动手下载的时机并不相同。另外，全仓测试**没有任何一处**硬编码 `PAGE_KEYS` 或 `show_page`（grep 0 命中），加一个 key 不会连带改测试断言。

### B. 筛选解释 → 记录页详情段

- 只改 `gui/collection_controller.py:714` 那段拼接（把"为什么"追加进去）与一个新增纯函数模块；`gui/collection_view.py:775-778` 的 `_detail_text` 与 `set_detail`(:1093) 保持原样——它已经是一个只读 Text 控件。
- 不新增表、不新增列、不新增 worker 调用。
- 未知码（历史行里可能出现新码表之外的字符串）显示为「未知原因（<原码>）」，**不许**猜成"被排除"。

## 2. 线程模型

现状（照抄，不发明）：

- `core/collection_desktop.py:154` `DesktopSession` 是唯一的 IO owner：一条线程 `collection-desktop-io`（:176/:187）、一个 `queue.Queue(4)`（:173）、每个命令返回 `Future`（`_command` :237）。
- `gui/collection_controller.py:243` `_poll` 每 tick 有 8 ms 预算，从 `_pending` 里取已完成的 Future 调回调，并带版本守卫（:255）。
- 面板只被控制器的 poll 调用（`set_verification_rows` :1398 的注释就写着"All data arrives through ... the controller's poll, never from a worker"）。

接线做法：

1. **读取器**（新文件 `core/crawl_state_reader.py`）：用 `sqlite3.connect("file:%s?mode=ro" % quoted, uri=True)` + `PRAGMA query_only=1`，只做 SELECT，暴露
   - `latest_run_for_account(account_id)`（新 SELECT：`crawl_runs` 按 `created_at DESC LIMIT 1`，不建 schema）
   - `get_run(run_id)`、`list_artists(run_id, limit=...)`
2. **绝不复用** `FollowCrawlStateRepository`：它的 `_connect()`（`core/follow_crawl_state.py:94-110`）会 `mkdir` 父目录、以读写方式连接、执行 `PRAGMA journal_mode=WAL`、并跑 `_create_schema()`（DDL）；`refresh_run_totals()`（:291-308`）是一条 UPDATE + commit。**这两件事都不允许出现在只读报告路径上。**
3. **worker 命令**：`DesktopRuntime`（`core/collection_desktop.py:93`）加 `coverage_summary(run_id=None)`，比照 `verification_summary`(:112)；`DesktopSession` 加同名薄封装（`:248` 附近，走 `_command`）。读在 IO 线程完成，返回 `FollowCoverage.as_dict()` + `text()` 的行列表（纯数据）。
4. **控制器**：新增 `self._watch('coverage', self.runtime.coverage_summary(...), callback)`，与 `verification_pages` 同一模式（:883-892）。
5. **铁律**：Tk 回调里不出现 `hashlib` / `open(` / `sqlite3` / `png_decode` / 任何 `.result()` 阻塞等待；面板只接收已经算好的 dict 与字符串。

## 3. 显示内容

### 3.1 `CoveragePanel`（新，比照 `gui/verification_panel.py`）

- 构造：`CoveragePanel(master, on_action)`，与 `VerificationPanel.__init__`(:296-315) 同形（唯一差异：本面板没有批量动作，`on_action` 只用于"刷新"）。
- 接口：`set_report(report, lines)`、`set_page_state(state=..., text=...)`、`dispose()`。
- 内容分区：
  1. **结论行**：`overall` 三态（complete / partial / unknown）+ `overall_reason`。措辞复用 `core/follow_coverage_text.py` 的既有中文，不另造。
  2. **三维行**：查找 / 判定 / 保存 各自的状态与 reason（同样来自 `render()`）。
  3. **正文**：直接渲染 `FollowCoverage.lines()`（`core/follow_coverage.py` 的 `lines()` → `follow_coverage_text.render`）。**文案只有一处实现**，面板不做句子拼接，只做只读 Text 展示（`state="disabled"`）。
  4. **未知清单**：`notes` 里"以下计数没有来源"那一行原样显示；计数为 `None` 的项在面板上显示「未知」，**永远不显示 0**。
  5. **未完成画师**：`incomplete` 的名单（最多 5 条 + 共 N 位），与 `notes` 一致。

### 3.2 三态（比照 `gui/verification_panel.py:37-42`）

| 状态 | 面板表现 |
| --- | --- |
| `idle` | 「尚未读取：还没有可显示的关注运行。」（未接线/没有 run 时就是这个，不是一个空白页） |
| `loading` | 清空旧内容（旧结论不许留在屏上，沿用 `set_page_state` :426-429 的做法），只显示「正在读取…」 |
| `error` | 「读取没有完成；请检查后点"刷新"重试。」+ reason；**不许显示成"没有覆盖"**（读失败 ≠ 空库） |
| `ready` | 显示 3.1 的内容 |

### 3.3 筛选解释（记录页详情）

在 `gui/collection_controller.py:714` 的详情文本末尾追加一段：

```text
为什么没有下载
- 收藏数低于阈值（bookmarks，证据 12）
- 字段未知（ai_flag）
```

- 逐条来自 `filter_reasons.translate(legacy_code)` → 新码 + 字段 + unknown；
- `unknown=True` 的原因写成「字段未知（<field>）」，**不写成"被排除"**；
- 没有 `reason_codes` 的行（例如下载成功的行）这一段整体不出现，不留空标题。

## 4. 测试策略

| 文件 | 覆盖什么 | 是否碰 Tk |
| --- | --- | --- |
| `tests/test_crawl_state_reader.py`（新） | 只读性：跑完后数据库文件字节不变、原本不存在的 `-wal`/`-shm` 没有出现、没有新目录；`mode=ro` 打不开 → typed error；`latest_run_for_account` 取最新一条 | 否 |
| `tests/test_coverage_panel_logic.py`（新） | 把 `as_dict()` 映射成展示模型：未知项显示「未知」、空 notes、`overall` 三态、`incomplete` 截断 | 否 |
| `tests/test_coverage_page.py`（新） | 页面装配：`PAGE_KEYS` 含 `coverage`、`show_page('coverage')` 后其他页隐藏、面板三态文案、`dispose()` 后不再接受 set | **是** |
| `tests/test_record_why.py`（新） | `reason_codes`→行文本：单码/多码/未知码/空列表；`unknown` 措辞 | 否 |

Tk 隔离做法（沿用现有）：

- 一个用例一个 root：`root = tk.Tk(); root.withdraw()`，用例结束 `destroy()`（与 `tests/test_collection_presentation.py` 的 fixture 同形）。
- 每个 Tk 模块在验收驱动里**单独一个进程**（`.runtime/jpgpng_u1b_u1c_20261114_01/probe/run_acceptance.py` 的 STEPS 已经这么分）。
- 合成夹具：临时 crawl 库用现成的 `FollowCrawlStateRepository.create_run` 在 `tmp_path` 里建（**写只发生在夹具里**），关闭连接后再用只读读取器读——这样被测代码本身永远不写。

## 5. 风险

1. **只读风险（最高）**：`FollowCrawlStateRepository` 会建目录、切 WAL、建表，`refresh_run_totals` 是 UPDATE。任何"顺手用一下现成类"的写法都会让只读报告变成写库。对策：新读取器 + "数据库字节不变"测试（这条能直接证伪）。
2. **Tk Tcl 初始化间歇故障**：本机 `tk.Tk()` 偶发 `can't find a usable tk.tcl`，同一进程里第 3 个 root 之后更容易出现，换用例换模块都会变。对策：(a) 新页面模块的用例控制在 3 个以内；(b) 面板构造中途失败必须 `dispose()` 掉半成品，避免坏状态传给下一个用例；(c) 验收驱动已对"只有 setup error、没有断言失败"的步骤做**记录式**重试，绝不掩盖断言失败；不要为了让某个模块变绿而反复挑记录。
3. **数据量**：`artist_progress` 可能上万行。读取器必须带 `LIMIT`（沿用 `list_artists(limit=...)` 的既有上限），面板显示前 N 位并明说"仅显示前 N 位"，不假装全量。
4. **版本漂移**：面板只消费 `as_dict()` 的既有键；计数键来自 `core/follow_coverage_counts.py` 的单一来源（`core/follow_coverage.py:31` 已经 import），新增计数键不需要改面板。
5. **文案分叉**：面板不拼句子，一律走 `follow_coverage_text.render`；记录页那段"为什么"也必须复用 `filter_reasons` 的词表，不许在 GUI 里再写一份中文映射。

## 6. 建议的落地顺序

1. `core/crawl_state_reader.py` + 只读性测试（CLI 与界面共用，先做）；
2. CLI 两个命令（已开 lane，需按只读纠正：不要用写类）；
3. `CoveragePanel` + `coverage` 页面（Tk，独立进程测试）；
4. 记录页「为什么没有下载」（纯函数，零 Tk 风险，可最后做）。
