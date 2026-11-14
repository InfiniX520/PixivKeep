# U1-B / U1-C 批次计划（冻结）

基线：`7feb7fe`（post-tidy 复审轮之后的候选）。分支 `feat/a1-u1b-u1c-20261114`。
上游设计：[DESIGN §3](../optimization/2026-11-11/DESIGN.md)、[验收与发布 §3.2/3.3](../optimization/2026-11-11/VALIDATION_AND_ROLLOUT.md)、[总表 U1-B/U1-C](../optimization/IMPLEMENTATION.md)。

## 0. 两条硬约束（比设计文档更严格，因为用户本轮明确要求）

1. **只在本目录工作**：`E:\Zero9\JuiLiu` 之内。共享解释器 `D:\Miniconda3\envs\JuiLiu` 属于**目录之外**，本轮**不安装、不升级、不卸载、不改环境变量、不建新环境**。
2. **能力缺失不得冒充通过**：没有解码组件时，深解码只能报"能力不可用"，不得把结构级结论升级成解码级。

因此 U1-B 本轮交付的是**只读清单 + 可重复检查器 + 回退材料规范**，不是环境变更；U1-C 本轮交付的是**分级证据 + 能力门 + 我们真的能做的解码（PNG，标准库）**，不是装一个解码器。

## 1. U1-B：共享依赖维护与发布可回退性（只读）

### 1.1 交付物

- `tools/maintenance/dependency_report.py`：只读报告器。输出（JSON + 文本）：
  `python`（版本/实现/环境路径/是否 conda）、`declared`（项目声明的依赖与版本约束，来自 requirements/pyproject 等文件）、`installed`（`importlib.metadata` 读到的版本）、`missing`/`unexpected`、`import_paths`（应用真正 import 的第三方模块解析到哪个文件）、`pip_check`（`pip check` 的输出与退出码）、`rollback`（当前包清单快照：名称+版本，作为回退材料的一部分）。
  只允许调用 `pip check`/`pip list --format=json` 这类**只读**子命令；代码里不得存在 install/uninstall/upgrade 路径。
- `docs/maintenance/2026-11-14_u1b_dependency_maintenance.md`：维护清单与三态记录。
  **三态必须分开写**：`advisory_matched`（是否核对过公告/CVE）、`upgrade_done`（是否真的升级）、`compat_checked`（是否跑过兼容检查）。本轮 `upgrade_done=false` 是**有意的**，因为约束 1。
  清单至少包含：解释器与来源、`pip check` 结果、受影响项目清单（JpgPng / arknights_story / zspms_story / File_Proc / wechat-mp-tools / xiaohongshu_corpus）、升级窗口条件（"不在其它在用项目运行时替换"）、回退材料（包清单快照 + 从何处可重装 + 需要用户确认的步骤）。
- `tests/test_dependency_report.py`：报告结构、三态字段、缺失依赖能被发现、模块里没有变更环境的代码路径（用 AST 断言不含 `install`/`uninstall` 调用）。

### 1.2 不做

不安装 Pillow 或任何包；不改 `requirements*` 的版本约束来"假装修好了"；不运行网络 pip；不新建环境。

## 2. U1-C：质量证明与深解码（分级 + 能力门）

### 2.1 证据分级（冻结的接口）

现有 `ImageEvidence.verification = "structure_checked"` 保持不变（历史行不重写）。新增并列维度，**不得互相替代**：

```text
QualityEvidence:
  source:    "declared_original" | "unknown"
  transport: "length_matched" | "unknown"
  structure: "ok" | "failed" | "undecided" | "unsupported"   # 现有 inspect_image
  decode:    "ok" | "failed" | "undecided" | "capability_unavailable"
  pages:     "all_frames" | "first_frame_only" | "unknown"
```

- `decode="capability_unavailable"` 是**一等公民**：策略要求严格解码而组件缺失时就报它，绝不写 ok。
- 任一维度 `failed` 只影响该维度；`undecided`（超限/取消/时间到）不等于损坏。

### 2.2 解码能力（本轮真的做的那部分）

- 新增 `core/decode_verification.py`：
  `decode_check(path, *, policy, limits, stop=None) -> DecodeOutcome`；
  `policy ∈ {"structure","strict"}`；`limits` 含 `max_bytes / max_pixels / max_frames / max_seconds`。
- **PNG：用标准库真正解码**（`zlib` 解压 IDAT → 逐行反滤波（None/Sub/Up/Average/Paeth）→ 校验总字节数与每行 filter 合法 → 得到完整像素流）。这是无外部组件的真实解码，输出 `decode="ok"` 或 `failed`（含 reason）。
- **JPEG/GIF/WebP**：本轮没有解码组件 → 返回 `capability_unavailable`（严格策略）或保持结构级（标准策略）。适配器接口留好（`Decoder` 协议 + 一个 `probe_decoder()`），但**不安装、不伪造**。
- 只读：不 `save()`、不旋转、不转码、不去元数据、不改 ICC；解码前后文件 `sha256` 必须一致（有断言）。
- 资源上限触发 → `undecided` + reason（`bytes_limit/pixels_limit/frames_limit/time_limit/cancel`），不是 `failed`。
- 收据：`DecodeReceipt`（format、frames、pixels、limits、decoder="stdlib-zlib-png"/"unavailable"、outcome、seconds、checked_at）。

### 2.3 接入

- `inspect_image(...)` 增加可选 `decode_policy`（默认 `"structure"`，保持现有调用方行为不变）；返回对象增加 `decode`/`pages` 字段。
- 库侧只做**只读展示**：维护 CLI 的 `scan` 行里带出 `decode` 维度（`--decode` 开关，默认关），不在本轮改 artifact 写入路径、不改 schema。

### 2.4 验收矩阵

| 用例 | 期望 |
| --- | --- |
| 合法 PNG（含隔行） | decode=ok，像素流长度精确 |
| 截断 PNG（IDAT 少一段） | structure=failed 或 decode=failed，不冒充完整 |
| 非法 filter 字节 | decode=failed（reason=invalid_filter） |
| 巨幅 PNG（尺寸超上限） | decode=undecided（pixels_limit），不是 failed |
| 多帧 GIF / JPEG / WebP（无解码器） | decode=capability_unavailable（严格）/ structure（标准） |
| 解码前后 sha256 | 完全一致（只读证明） |
| 取消/超时 | decode=undecided（cancel/time_limit），保留现场 |
| 历史行 | `verification` 不变，不被批量升级 |

## 3. 归属与并行

| lane | 独占文件 |
| --- | --- |
| U1-B | `tools/maintenance/dependency_report.py`、`docs/maintenance/2026-11-14_u1b_dependency_maintenance.md`、`tests/test_dependency_report.py` |
| U1-C | `core/decode_verification.py`、`core/image_validation.py`、`tests/test_decode_verification.py`、`docs/optimization/2026-11-14/U1C_QUALITY_EVIDENCE.md` |
| 主线程 | 契约/计划、`maintain.py` CLI 接线、文档导航、`TASK_PLAN`/`LOG`、合流与验收 |

两条 lane 文件不重叠；主线程只在合流后碰 `core/library_maintenance.py` 与文档。

## 4. 本批次完成定义

1. U1-B：报告器可跑、三态诚实、有测试、文档写明"未升级"以及为什么；
2. U1-C：PNG 真解码通过矩阵、其它格式诚实报"能力不可用"、只读证明；
3. 全量矩阵与三个 Tk 模块按既定口径重跑，不为新代码放宽任何旧断言；
4. 只读复审（增量）无 P1/P2。

不在本批次：安装任何组件、真实图库解码、GUI 新增页面、schema 变更。

## 5. 小节点拆分（一个节点做完并验收，再进下一个）

大节点不一次交付。每个小节点必须自带完成定义与证据，验收通过才进下一个；任何一步失败就停在那一步，不往下堆。

### U1-B（4 个小节点）

| 步 | 交付 | 完成定义 / 证据 |
| --- | --- | --- |
| B1 事实清点 | 解释器、环境路径、项目声明文件清单、已安装包清单（名称+版本） | 一份原始快照 JSON；不含任何改写、不做判断 |
| B2 差异与风险 | missing / unexpected / import 解析路径 / `pip check` 结果 | 报告器可跑；单测覆盖「声明了但没装」「装了但没声明」「`pip check` 非零」 |
| B3 三态与回退材料 | 文档：advisory_matched / upgrade_done / compat_checked 三列 + 回退清单与窗口条件 | 三态字段有测试；文档明确本轮 `upgrade_done=false` 及原因 |
| B4 真实只读跑一次 | 把 B1–B3 在真实环境跑一遍（只读） | 结果写进文档；AST 断言代码里没有 install/uninstall/upgrade |

### U1-C（4 个小节点）

| 步 | 交付 | 完成定义 / 证据 |
| --- | --- | --- |
| C1 证据模型 | `QualityEvidence`（source/transport/structure/decode/pages）+ `DecodeReceipt` | 纯数据 + 单测；**不改** `inspect_image` 现有行为 |
| C2 PNG 解码器 | zlib 解 IDAT → 逐行反滤波（None/Sub/Up/Average/Paeth）→ 每行 filter 与总长校验 | 合法/截断/非法 filter/隔行 四类用例；输出 ok/failed(reason) |
| C3 资源上限与取消 | max_bytes/max_pixels/max_frames/max_seconds + stop | 超限/取消 → `undecided`（带 reason），不是 `failed`；逐项用例 |
| C4 集成与只读证明 | `inspect_image(..., decode_policy=)`，默认 structure；其它格式 `capability_unavailable` | 矩阵全过；解码前后 `sha256` 一致；历史行 `verification` 不被改写 |

### 顺序与并行

- U1-B 与 U1-C 两条 lane 可并行（文件不重叠），但**各自内部按 B1→B4、C1→C4 串行**，每步单独提交、单独验收。
- U2-A / U2-B 不在本批次开工；等 U1 组验收后再拆它们的小节点。
