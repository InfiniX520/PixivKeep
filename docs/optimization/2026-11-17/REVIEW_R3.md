# 第三轮只读复审（RA-01…RA-06）与处理

复审者是继承本轮上下文的只读 fork（`review-r3`），不改已跟踪文件、不开真实库。
原始意见：`.runtime/jpgpng_ra_fix_20261117_01/verify/REVIEW_R3_raw.md`（23,053 B）。

## 复审自己复跑的结果

| 检查 | 结果 |
| --- | --- |
| 相邻 13 条 | 12 通过 / 1 失败（RA-04 #1 停在**第 62 行**、`detail='review_not_stored'`，与 §10.1 裁决一致） |
| 冻结 15 条 | 15/15 |
| 九个新 RA 模块 | 127/127 |
| `tests/test_decode_cli.py` | 15/15 |
| 五组最终 XML 的时间戳 | 全部晚于当时代码 HEAD 的提交时间，可作同提交证据 |
| `git diff 5d1e676..HEAD -- docs/audits tests/test_boundaries.py` | 空 |
| 提交里是否含库/账本/cookies/临时目录 | 无 |

它给的一条方法论提醒值得保留：**提交落地后必须重跑**。它第一次读到的 `final_body.xml` 是比 HEAD 早 37 秒的旧跑，
里面正好有随后被修掉的那条失败；集成人重跑后覆盖。旧 XML 不能当同提交证据。

## 处理结果

| 编号 | 复审意见 | 处理 |
| --- | --- | --- |
| P1 | 两份文档矩阵仍是 `<!--MATRIX-->` 占位，`CLOSURE_R3.md` 指向的 `REVIEW_R3.md` 不存在 | **已填**（本文件与两份文档的"验证矩阵"节）；数字取自 `run_round.py` 的 XML 解析，不是手抄 |
| P2-1 | 旧 crawl 状态库的覆盖命令仍会新建 `-wal/-shm`，却打印"只读，legacy" | **已修** `2aa18c3`：旧库改走 `read_only_connection`（干净关闭的 WAL 库 → 私有副本），文本改由实测的 read plan 说明；新增 4 条用例（副本/直读/缺库/缺表） |
| P2-2 | `samples.review` 会存下永远不能用的"已看过"（未带 `rules_fingerprint`） | **文档已改**：写清"不带指纹的 review——无论新旧——消费时一律 unknown"；**写入端拒绝留待下一轮**，因为它要改审阅命令的规则来源（产品决定），本轮 E 包的契约明确不动写回命令 |
| P2-3 | `CLOSURE_R3` 说"上表每一行都有 CLI 与 DesktopRuntime 两条调用"，实际只有副本一行有端到端 | **已改**：改成"每一行有计划层断言；副本一行有 CLI/Desktop 端到端；其余由同一条 `read_only_connection` 覆盖"，并指出不夸大成每行端到端 |
| P2-4 | 残余窗口写轻了：闸门只能看见**仍存在**的外来名，被 SQLite 删掉的注入文件无从拒 | **已改**（写得更重）："无法证明与无法阻止"，并写明发布闸门看不见已被删掉的名字 |
| P3-1 | `finish()` 用未扣解码字节的余额补读，接流式解码器时单次检查可读约 2×上限 | **已修** `2aa18c3`：解码字节先计费再补读；新增 `tests/test_ra06_budget_ceiling.py`，用"同一进程并排加载 HEAD 版本模块"证明红→绿——旧顺序在 300 字节上限下实测读到 **400 字节**并把内容标成 `observed`，修后总账停在 300、内容标为不完整 |
| P3-2 | 契约 §10.2 的许可清单漏了 D 包改的 `test_counts_are_real_numbers_not_placeholders` | **已补** §10.4：把三处被批准的断言改写集中列出（含集成人改的 `test_decode_cli` 助手），并写明"断言可以换更强的说法，不能把期望值调低" |

## 复审对证据强度的结论（保留原文要点）

- 既有断言改动**没有一处是为让红变绿而调低期望**；四个夹具新增 `account_id` 与生产的 `organizer/snapshots.py` 一致，
  不是造证据；`Store` 替身只加了 `get('review')`，`sufficient` 门槛没放宽。
- 未知没有被写成 0：`artists_total`、`artists_members`、`content.bytes`、`bytes_read`、`read.copy.*`、`family.acl`、
  `rules_fingerprint`、`proof_limits.account` 逐项核对通过（唯一形状问题是 `works_multi_batch` 等用缺键表达 0）。
- 选择表的"measured"注释都有 `runs/c/evidence/probe_read_modes*.txt`、`probe_cost.txt` 支撑，150 MB 的耗时与关闭报告一致。

## 复审后仍然存在的、被接受的风险

1. 备份 staging 目录的预检→connect 窗口（同用户进程主动竞争时无法证明、无法阻止；见 CLOSURE_R3 §RA-01）。
2. 副本一致性只比较 (size, mtime_ns)，不重读源；同尺寸改写并精确恢复 mtime 的 ABA 逃得过去。
3. `maintain inspect/verify` 仍直接以 `mode=ro` 打开源库，会留下同类旁车（模块 docstring 已写明，本轮未改）。
4. `organizer/plans.py` 建计划入口不校验规则指纹；`samples.review` 不拒绝无指纹记录。
5. RA-04 反例 #1 按 §10.1 保持失败（它是过期反例，不是遗留缺陷）。

## 复审后的结论

修完上表后重新跑矩阵（`final2_*`，见两份文档的"验证矩阵"节）：六项实现、五组证据、冻结边界都成立；
仍有 1 条相邻反例失败，且它是 §10.1 裁决过的过期反例——这一点必须在给用户的报告里原样保留，不能写成"13 条全绿"。
