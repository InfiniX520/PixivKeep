# 五个大节点的拆分与推进顺序（先拆，再一步步做）

基线：`b01437c`（候选分支 `feat/a1-u1-u2-20261114`）。本文件是**总拆分**；单个节点的细节在各自计划里冻结后才开工。

## 0. 规矩

1. **一次只做一个大节点。** 同一节点内最多两条 lane，且文件不重叠；节点之间串行。
2. **节点内部按步走**：一步一提交、一步一验收；某步不过就停在那步修，不往下堆。
3. 每步必须有"完成定义 + 证据"，证据要能贴出来（测试数字、报告文件、结构断言）。
4. 三个审批点：**U1-B+U1-C 一次**、**U2-A+U2-B 一次**、**E1-3+E1-4 一次**。其余我自决。
5. 只在本目录工作；不装包、不动共享解释器。

## 1. U1-B 依赖与发布可回退性（4 步，只读）

已知事实（我读过）：仓库里有 `pyproject.toml`（isort/black/pytest 配置，无依赖声明）、`requirements.txt`（pixivpy3、requests、pyyaml、selenium、cloudscraper、requests-toolbelt）、`requirements-dev.txt`、`requirements-build.txt`；`core/gui/utils` 里第三方 import 以 `requests` 为主。已装环境：Python 3.12.14，`PIL`/`numpy` **未安装**，`pip check` 干净。

| 步 | 交付 | 验收 |
| --- | --- | --- |
| B1 清点 | 解释器与环境路径、四个声明文件的解析结果、已安装包清单、`core/gui/utils` 的第三方 import 与实际解析路径 | 一份只读快照 JSON；不做判断、不改写 |
| B2 差异 | `declared / installed / imported` 三者对照：missing、unexpected、declared-but-unused | 报告器可跑；三条单测（声明没装 / 装了没声明 / `pip check` 非零） |
| B3 三态与回退 | `advisory_matched / upgrade_done / compat_checked` 三列 + 回退清单 + 升级窗口条件 | 三态有测试；AST 断言模块内无 install/uninstall/upgrade |
| B4 真实只读跑 | 在真实环境跑一遍并把结果写入文档 | 文档写清 `upgrade_done=false` 与原因；给出"要升级需用户同意什么" |

## 2. U1-C 质量证据与深解码（4 步）

已知事实：`core/image_validation.py` 只做结构级（含 PNG 的 IDAT 解压与行结构），`inspect_image` 只有 **2 处**调用点；`ImageEvidence.verification="structure_checked"` 是既有语义。

| 步 | 交付 | 验收 |
| --- | --- | --- |
| C1 证据模型 | `QualityEvidence`（source/transport/structure/decode/pages）+ `DecodeReceipt` | 纯数据 + 单测；**不改** `inspect_image` 行为 |
| C2 PNG 真解码 | zlib 解 IDAT → 逐行反滤波（None/Sub/Up/Average/Paeth）→ 每行 filter 与总长校验 | 合法/截断/非法 filter/隔行 四类用例；ok 或 failed(reason) |
| C3 上限与取消 | max_bytes/max_pixels/max_frames/max_seconds + stop | 超限/取消 → `undecided`(reason)，不是 failed |
| C4 集成与只读证明 | `inspect_image(..., decode_policy=)` 默认 structure；JPEG/GIF/WebP → `capability_unavailable` | 矩阵全过；前后 `sha256` 一致；历史行不被改写 |

## 3. U2-A 关注历史覆盖摘要（3 步）

| 步 | 交付 | 验收 |
| --- | --- | --- |
| A1 事实清点 | 只读列出 `follow_*` 真正能提供的事实（画师进度、失败页、预算停止、重复分页、断点） | 一份事实清单；不写库 |
| A2 覆盖对象 | `FollowCoverage`：三维状态 + counts + stop_reason + 未知分母输出 null | 三条不变量各有测试（未查完不得说查完 / 三维独立 / 分母不估算） |
| A3 文档与矩阵 | 口径说明 + 示例文本（标示例）+ 五类场景 | 部分失败、预算停止、重复分页、全被过滤、私密范围缺失 全过 |

## 4. U2-B 筛选解释与离线重评估（4 步）

| 步 | 交付 | 验收 |
| --- | --- | --- |
| B1 原因码与解释 | 固定原因码表 + `explain()`（match/exclude/unknown + 字段证据） | 三态用例；未知不当 0；`type_unsupported` 与 `field_unknown` 不混 |
| B2 离线重评估 | `reevaluate()`（放宽纳入 / 收紧只标记） | 放宽 → `newly_included` 且不重下；收紧 → 不删文件 |
| B3 方案快照 | `FilterPlan` 不可变 + 运行读快照 | 运行中改方案不影响本次；有测试 |
| B4 文档与矩阵 | 口径 + 原因码表 + 示例 | 矩阵全过 |

## 5. E1-3 / E1-4 整理 CLI（步骤待细读设计后冻结）

我只读到 [DESIGN §E1](../../refactor/detailed_design/08_FOLLOW_ORGANIZER_EXTENSION.md) 的概要（小样 → 分类规则 → 证据 → 计划 → 网站写回/读回，保留人工修改、冲突不覆盖），**具体步骤要先把该设计与现有 `organize.cmd` 读透再冻结**，避免凭印象拆。暂定骨架：

| 步 | 内容 |
| --- | --- |
| E1 现状清点 | `organize.cmd` / E1-2 已交付能力与边界（只读清点） |
| E2 E1-3 小样 | 多图分类小样：样图 → 规则 → 证据（只读，不写网站） |
| E3 人工复核入口 | 冲突/不确定项的人工处理与回执 |
| E4 E1-4 分批 | 分批整理：计划 → 执行 → 回执（写回按设计门槛） |
| E5 文档与验收 | 口径、示例、矩阵 |

## 6. 顺序

```text
U1-B(B1→B4) 与 U1-C(C1→C4) 并行两条 lane
        ↓ 合流 + 全量矩阵 + 只读复审
   【审批点 1】
U2-A(A1→A3) → U2-B(B1→B4)（一个节点做完再下一个）
        ↓ 合流 + 矩阵 + 复审
   【审批点 2】
E1-3 → E1-4（先细读设计再冻结步骤）
        ↓ 合流 + 矩阵 + 复审
   【审批点 3】
```

## 7. 现场状态

之前那两条 lane 被我在"重新拆"时停掉了，各留了未提交的半成品：U1-B 的 `tools/maintenance/dependency_report.py`、U1-C 的 `core/decode_verification.py` 与 `image_validation.py` 改动。**重开时不直接采信**：先按 B1/C1 的验收标准审，能对上就复用，对不上就重写。
