# S0 契约（第三轮）：六个证据缺口的冻结接口

基线：审计分支 `audit/u1u2-e1-followup-20261117` @ `5d1e676`（候选实现 `944ee13`）。
本文件由主智能体在 fork 之前写定，之后各包按此实施；任何改动必须先改本文件并在提交信息里说明原因。
反例固定为 `docs/audits/2026-11-15-u1u2-e1-r2-independent/evidence/test_adjacent.py`（sha256 `7c7914066dca4166cb1eccd75b6e0ddf2dcb2dbe3abe8d1ef6a7b7a9ac7c0214`），
旧 15 条冻结反例不变（`docs/audits/2026-11-15-u1u2-e1-independent/evidence/test_boundaries.py`）。

## 0. 本轮开始时实测（不是推断）

| 事实 | 证据 |
| --- | --- |
| 13 条相邻反例在 `5d1e676` 上：**10 失败、3 通过、0 设置错误**，23.2 s | `.runtime/jpgpng_ra_fix_20261117_01/verify/ra_before.xml` |
| 失败项 | RA-01(-journal)、RA-01(-wal)、RA-02、RA-03×3、RA-04×2、RA-05、RA-06 |
| 通过项（对照） | 缺 visual 的低置信、applied 不可回退、纯标签不冒充审阅 |

各包在动代码前必须先在本机重复一次同一命令、确认同一结果，再开始修改；修完用同一命令、同一 XML 位置复跑。

## 1. A / RA-01：目标文件族的写入授权

**产品变化**：`backup_library` 在写目标库之前，先取得目标**整个文件族**（`<name>`、`-journal`、`-wal`、`-shm`）的授权；SQLite 只被允许在本次运行的私有目录里创建/回收成员；成品目录以一次原子改名发布。

**冻结对象** `TargetFamily`（`core/workspace_ownership.py`，名称可调，字段冻结）：

```
{"root": Path,            # 本次运行创建并独占的目录（staging）
 "target": Path,          # root/<source.name>
 "staging": Path,         # == root
 "names": {"": "ours", "-journal": "absent|ours|foreign", "-wal": ..., "-shm": ...},
 "precheck": "pass" | "refused:<names>",
 "acl": "restricted" | "degraded",      # 见下
 "published": bool}
```

**不变量（缺一不可）**

1. **SQLite 永不打开以成品名命名的数据库**：目标库在私有 staging 目录内创建；成品目录（`destination_root/<stamp>_<label>/`）只由一次 `os.rename` 原子出现。因此成品名下的任何外来文件都不会被 SQLite 打开、回滚或删除。
2. **连接前预检**：staging 内三个旁车名任一存在 → 拒绝（`backup_target_family_occupied`），外来文件保持字节不变，不写任何 manifest，不发布目录。
3. **连接后复核**：SQLite 关闭后，staging 内允许出现的名字只有 `target` 与本次 claim 记录的成员。出现别的名字 → 拒绝（`backup_target_family_foreign`），保留全部外来名字，不发布。
4. **发布失败即不发布**：`os.rename` 到已存在的成品目录必须失败（`backup_publish_failed`），绝不覆盖、绝不合并、绝不“先删再放”。
5. **禁止削弱一致性**：不得用 `journal_mode=OFF/MEMORY`、`synchronous=OFF`、`pragma locking_mode=EXCLUSIVE` 之类方式绕开文件族问题。
6. **失败清理只碰自己**：只按记录的身份回收本 run 创建的成员（沿用 `_unlink_proven` 语义），外来名字原样保留并在错误里列出（`kept=[...]`）。
7. **ACL 硬化（能则做，不能则记录）**：staging 目录创建后尝试限制继承 ACL（仅当前用户 + SYSTEM），成功记 `acl=restricted`，失败记 `acl=degraded` 并写进 manifest 事实。`degraded` 不阻止备份，但必须在结果与维护记录里可见。

**发布顺序**：copy → 写 staging 内 manifest（`manifest.json`，内容不变，仅加 `family` 段）→ 原子改名 → 只在改名成功后返回成功结果。

**错误码**（新增，旧码全保留）：`backup_target_family_occupied`、`backup_target_family_foreign`、`backup_publish_failed`、`backup_staging_failed`。

**验收矩阵**：主文件替换（lease 前/后）；三个旁车在 connect 前 / backup 中 / close 前出现；空与非空；普通文件与硬链接；目标目录被换位；失败与取消；`file_group` 模式；`backup → verify` 整链；旧 EA-01 与 W1 15 例。证明记录必须包含**真实 SQLite 调用**（不得全 mock），并给出一次“外来哨兵字节与 mtime 前后一致”的实测。

**仍未知**（必须写在报告里）：同用户恶意进程监视 staging 目录并在预检与 connect 之间注入文件——我们能事后拒绝发布，但 Windows 上无法阻止 SQLite 删除它已打开的旁车名。这是本包的明确边界，不得写成“已完全防止”。

## 2. B / RA-06：质量回执绑定真正解码的内容

**产品变化**：`check_file`/`quality_evidence` 的结构检查、解码、回执绑定到**同一个描述符读出的字节**，并在报告里带内容观察对象；解码成功不再只凭“路径前后两次哈希相同”。

**冻结对象** `content`（报告新增顶层键）：

```
{"state": "observed" | "unknown" | "mismatch",
 "sha256": str|None,      # 从同一描述符读到的字节的摘要
 "bytes": int|None,       # 实际读到的字节数
 "complete": bool,        # 读满整文件（bytes == size）
 "source": "file_stream",
 "reason": "" | "unreadable" | "bytes_limit" | "cancel" | "partial_read"}
```

**判定规则**

- `quality.decode.state == "ok"` 当且仅当：`content.state == "observed"` 且 `content.complete` 且 `content.sha256 == sha256_before == sha256_after`。任一不成立 → decode 降为 `undecided`，reason 为 `content_unbound`（读不全）或 `content_changed`（摘要不一致）。
- 结构检查、解码、摘要必须来自同一份内容：打开一次描述符，结构/解码消费该流（或该流读出的有界缓冲），摘要对同一批字节计算。禁止“先按路径哈希、再按路径解码”。
- 读量全部走现有 `ReadMeter`/`CommandBudget` 计费；内存不得无界（上界 `limits.max_bytes`）；取消按块检查。
- `bytes_read` 缺失仍是 `None`，不得当 0。
- `_decode_outcome` 只在 decode ok 且内容绑定成立时返回 0（该行由集成人在 A 合流后串行接入 `core/library_maintenance.py`）。

**验收**：RA-06 反例；固定合法/非法 PNG；A→B→A 同尺寸同 mtime；原地写；路径替换；读取/哈希/解码三阶段分别取消；预算恰好不足最后一阶段；`bytes_read` 未知；第三方 decoder 不存在 → `capability_unavailable` 且非成功；CLI exit code 与 JSON/文本一致；W2 的 256 KiB 取消上界与按块计费不回退。

**仍未知**：核验结束后其他进程继续改文件——本包不承诺，也不得写成“文件之后不可变”。

## 3. C / RA-02：活跃库读取与严格无副作用读取

**产品变化**：`core/follow_facts_v2.py` 的连接层返回**读计划与证据**，不再用“有没有旁车”推断 immutable；`coverage_report_v2` 的记录里写明本次到底是“SQL 只读”还是“源目录零副作用”，以及依据。

**冻结对象** `ReadPlan`（随连接一次返回，不做事后目录复查）：

```
{"mode": "ro" | "ro+immutable" | "copy",
 "sql_mode": "ro",                     # copy 模式下底层仍是 ro，事实不省略
 "immutable_source": "" | "caller_owned_snapshot",
 "journal": "wal" | "rollback" | "unknown",
 "sidecars": {"-journal": bool, "-wal": bool, "-shm": bool},
 "promise": "names_and_bytes" | "names_only" | "copy_verified",
 "strict_no_side_effects": bool,        # 仅 names_and_bytes 与 copy_verified 为 true
 "copy": {"dir": str, "members": {name: sha256}, "verified": bool,
          "source_members": {name: [size, mtime_ns]}, "reason": str} | None,
 "evidence": {"checked": [...], "created": [...], "removed": [...], "changed": [...]}}
```

**选择表（必须先逐项实测，再定稿）**

| 源状态（lane C 实测，2026-11-17） | 采用 | 允许的副作用 | 证据承诺 |
| --- | --- | --- | --- |
| rollback，无 `-journal` | `ro` + 一个读事务 | 无 | `names_and_bytes` |
| rollback，0 字节 `-journal` | `ro` | 无 | `names_and_bytes` |
| rollback，`-journal` 有内容但非热 | `ro` | 无（不回滚、不改名） | `names_and_bytes` |
| rollback，**真热 journal** | 拒绝 `source_hot_journal`（只读连接会报 attempt to write a readonly database） | — | — |
| WAL，`-wal` 与 `-shm` 都在（写者活或残留） | `ro` + 读事务，参与锁协议；写者可并发提交 | 不新建、不改名，但 `-shm` 锁字节会变 | `names_only` |
| WAL，`-wal` 在、`-shm` 不在 | **私有副本**（直接 `ro` 会新建 `-shm`；`immutable=1` 会读到 stale 数据） | 只在私有副本目录 | `copy_verified` |
| WAL，两者都不在（干净关闭） | **私有副本**（实测直接 `ro` 会新建 `-wal`(0 B) 与 `-shm`(32 KiB)） | 只在私有副本目录 | `copy_verified` |
| 文件缺失 / 目录 | `database_missing` | 无 | — |
| 非数据库文件 | `database_unreadable`（连接成功、查询报 file is not a database） | 无 | — |
| 调用方显式声明这是我自己的冻结副本 | `ro+immutable`（`immutable_source="caller_owned_snapshot"`） | 无（也不参与锁） | `names_and_bytes` |

- `immutable=1` 只能由调用方显式声明“这是我自己的冻结副本”时使用（`immutable_source="caller_owned_snapshot"`），永远不由“旁边没有文件”推断。
- **私有副本是默认允许的**（`allow_private_copy=True`）：冻结反例用无参 `read_only_connection(path)` 调用并期望读到数据且源目录不变，实测只有副本能同时满足。副本必须：复制主文件与存在的 `-wal`；复制前后比较每个成员 (size, mtime_ns)，并确认没有新出现的 `-shm`/`-journal`/`-wal`，任一变化即 `source_changed_during_copy`（非零、不返回数字）；副本目录在连接关闭时删除，只碰自有目录；副本读取同样在一个读事务内完成。文档要写清这一状态（WAL 且无 `-shm`）的代价是一次全量复制。
- 文档与报告措辞必须区分两个承诺：`sql_read_only`（不执行业务写）与 `source_unchanged`（名字集合 + 摘要 + 属性不变）。只有实测通过才可说 `source_unchanged`。
- 正常锁定读允许写者并发提交：不得把并发写一律报 `database_changed_during_read`，也不得因此返回混合视图（读事务内完成）。
- 关闭连接后不得因“再查一次目录”改变结论。

**验收**：上表每一行都有 CLI 与 DesktopRuntime 两条调用；比较源文件族名字/摘要/属性；确认无隐式 WAL/SHM 创建、无 checkpoint、无删除；并发提交与 rollback 写者各一例；无法证明时返回明确 unknown/拒绝而不是假证明。

## 4. D / RA-03：按 V2 关系重建覆盖事实

**产品变化**：`coverage_facts` 不再“遍历所有 batch_items 并按 job 取 delivery”；改为沿 job → 批次 → 关注快照/进度 → 作品 → 页面链接 → delivery/artifact 的真实关系取数，并区分 job 口径与批次口径。

**冻结规则**

1. **job → snapshot**：用 `events` 里该 job 最近的 `kind='follow_snapshot_selected'`（payload 里有 `snapshot_id`）确定本次采用的快照。没有该事件时：非 follow 任务 → `snapshot_scope="not_applicable"`；follow 任务 → `snapshot_scope="unknown"`，`artists_total=None`，**绝不写 0**。
2. **画师总体**：`follow_snapshot_members` 的成员是该 job 的冻结集合；其中没有 `batch_items` 行的画师必须以 `works_seen=0` 出现，状态取 `not_visited`（有快照证据但本 job 没走到）或 `pending`。统计新增：`artists_visited`、`artists_total_state`、`unknown_artists_reason`。
3. **作品去重**：同一 job 内按 unique `illust_id` 计 `works_seen`；续跑/多批次不得重复计数；`works_multi_batch` 记多批次出现的作品数。
4. **批次顺序**：同一作品多批次时按 `batches.created_at, batch_id` 的次序决定代表性行（后出现的批次覆盖先前的 pending/失败结论），不按字符串随机取胜；同一作品同代次冲突 → `works_conflict`。
5. **页面链接**：expected 取自代表性批次的 `required_pages_json`/`page_count`，按 (illust_id, page) 去重；observed 沿 `batch_item_pages` 解析：`delivery_id` → `deliveries.state`，`artifact_id` → `artifacts.sha256`，**允许引用其他 job 的 artifact**（`artifacts.job_id != job_id` 的合法历史成果计入，条目标 `origin_job="other"`，`link="artifact"`）。
6. 缺期望内容代次继续 `unknown`（`expected_content_version_missing`）；不得把 observed 摘要复制成 expected，不得把 URL 身份当内容 hash。

**验收**：本轮三条 RA-03 反例；空画师、零关注、部分快照、指定画师、筛选、分页失败、跨批次重试、跨任务复用、两代冲突、外任务活动 pending；GUI 当前选中任务与 CLI 得到相同 scope/总数/原因；1k 与 10k 作品规模；避免无界 IN 参数（>900 个 id 需分批或临时表）；记录查询语句数、峰值内存、响应与取消。

## 5. E / RA-04：canonical 审阅记录与消费时版本门禁

**产品变化**：`review_evidence` 以 `receipt_id` 读取**已落库**的 review，并与传入副本比对；`classify` 在消费时核对证据的规则身份，过期或不匹配一律不能 high。

**冻结接口**

```
review_evidence(store, receipt, *, current_rules_version="", current_snapshot_id="",
                current_rules_fingerprint="") -> VisualEvidence
VisualEvidence(+ review_id: str, (+ rules_fingerprint: str))
samples.review(..., payload 增加可选 rules_fingerprint)
classify(evidence, ruleset, *, visual=None)  # 内部核对 visual.rules_version / rules_fingerprint
```

**规则**

1. `receipt['id']` 必须存在且 `store.get('review', id)` 能取到；取不到 → `unknown/visual_evidence_unverified, detail=review_not_stored`。**未落库的自由字典永不 sufficient。**
2. 传入副本与已落库 review 在身份字段（account_id、artist_id、snapshot_id、sample_id、viewed、每条 evidence 的 sha256/illust_id、needs_more、rules_version、attestation）上必须一致；不一致 → `unverified, detail=receipt_copy_mismatch`（detail 里给两边摘要）。
3. `VisualEvidence.rules_fingerprint` 来自已落库 review；缺失时 **state 必须降为 `unknown`，detail 为 `visual_rules_unbound`**。不允许保留 `state="sufficient"` 而只让属性返回 False：那样同一对象自相矛盾，读 state 的消费者仍可绕开。
4. `classify` 校验：`visual.rules_version == ruleset.version` 且 `visual.rules_fingerprint == ruleset_fingerprint(ruleset)`；任一不符 → 视同 stale，不能 high。`changed_by_version` 走同一门禁。
5. 现有门槛全部保留：sample 对齐、attestation、每页 sha256、不同作品计数、多页折叠、`available_works` 未知、快照过期、缓存摘要。
6. `samples.review` 写新 review 时记录 `rules_fingerprint`（调用方给 `RuleSet` 或指纹字符串）；旧记录缺该字段 → 消费时 unknown，不补写、不改历史 review。

**验收**：sample-only 无 review；同 id 篡改副本；review 指向别的 artist/account/sample；真实 `samples.review` 正常记录；同作品多页凑数；available_works 未知；快照更新；v1 证据给 v2；版本标签相同但规则内容不同（指纹不同）；序列化往返。失败时保留记录、不重写历史。

## 6. F / RA-05：账号身份沿 scope → report

**产品变化**：`build_scope` 把 `snapshot.account_id` 带进 scope 并在导出/恢复后保留；`build_report` 先判身份再判包含关系；来源未知则报告 unknown 且 CLI 非 0。

**冻结字段**

- `GroupScope.account_id`（属性 + `as_dict`/`from_dict`），`source["account_id"]`、`source["account_id_state"]="recorded"|"not_recorded"`。
- `source["identity_fingerprint"] = sha256({account_id, snapshot_id, group_pairs, members})`；成员集指纹 `fingerprint` 语义不变（仍是成员集摘要）。
- 报告 `unknowns` 新增 `scope_account_unknown`；`covers_batch` 仍按包含关系计算，但整体状态不得为 complete。

**规则**

1. scope 账号已知且与 batch 账号不同 → `account_scope_mismatch`（直接拒绝）。
2. scope 账号未知（旧文档或快照无 account_id）→ 不补当前账号、不隐含一致：报告记 unknown，CLI 退出非 0。
3. `--account` 只核对调用上下文，不证明 scope 来源（文档必须这么写）。
4. `ensure_current` 增加账号核对：同一快照换了账号 → `scope_account_changed`。
5. `website_write_back=not_proven_here` 的既有限定保留。

**验收**：相同 snapshot 标签不同账号；正常默认空 snapshot_id 的 batch 配另一账号范围；`scope-plan` 写 JSON 再 `batch-report` 读取整链；直接 API、`from_dict`、`--account`、`--superset-ok`；同账号正常对照；旧无身份格式。退出码与 unknowns/文本一致。

## 7. 文件所有权（并行边界，同一文件不得两 lane 同改）

| 包 | 独占文件 | 集成人保留 |
| --- | --- | --- |
| A | `core/workspace_ownership.py`、`core/library_maintenance.py`（backup 段）、`tests/test_w1_online_backup_lease.py` 新增、`tests/test_ra01_*` | 合流、`_decode_outcome` 串行接入、CLI |
| B | `core/quality_check.py`、`core/decode_limits.py`、`core/png_decode.py`、`core/image_validation.py`、`tests/test_w2_budget_accounting.py` 增量、`tests/test_ra06_*` | 同上 |
| C | `core/follow_facts_v2.py`（连接层）、`core/report_commands.py`、`tests/test_ra02_*` | 合流、桌面/GUI 接线 |
| D | `core/follow_facts_v2.py`（事实 SQL；**C 合流后才开工**）、`core/follow_coverage_counts.py` 必要处、`tests/test_v2_coverage_facts.py`、`tests/test_ra03_*` | 合流、图形界面 |
| E | `organizer/rules.py`、`organizer/samples.py`、`organizer/store.py`（只加读取接口）、`tests/test_w5_visual_evidence.py` 增量、`tests/test_ra04_*` | 合流 |
| F | `organizer/group_scope.py`、`organizer/report.py`、`organizer/offline_cli.py`、`tests/test_w4_group_scope.py`/`test_w5_report_scope.py` 增量、`tests/test_ra05_*` | 合流 |

共同禁区：`docs/audits/**`（冻结）、`tests/test_boundaries.py`（冻结）、任何真实数据库/图库/`organizer_data`/账号；不改 DDL；不装依赖；不新增网络调用。

## 8. 证据与验收门槛（每包都要交）

1. 开工前复现本条反例红（命令 + XML 路径）。
2. 修后同一反例绿（同一命令、新 XML），并给出相邻边界用例（不只重放给定输入）。
3. 五栏表：产品变化 / 公开入口 / 已证明范围 / 仍未知范围 / 相邻反例。
4. 全绿门槛：旧 15 + 新 13 + 本包新增测试；合流后整树、编译、Tk、presentation。
5. 不得把“第一次红、第二次绿”写成环境根因；不得把 API 测试通过写成 CLI/GUI 也接线。

## 9. 本轮不做

真实账号写回、图库恢复/释放、云盘往返、安装解码器、V2 增加期望内容代次（独立数据面工作）、把 observed 摘要当 expected、任何形式的“通过/完成/保证”而没有同提交证据。

## 10. 契约修订记录

### 10.1 RA-04 的两条反例互斥（2026-11-17，lane E 开工时发现，lead 裁决）

- 反例 #1 `test_ra04_visual_receipt_cannot_be_reused_under_new_rule_version` 第 62 行断言
  `review_evidence(Store({'s1':sample}), receipt, ...)` 返回 sufficient，而该 store 里**没有任何 review 记录**；
- 反例 #2 `test_ra04_visual_adapter_requires_a_stored_review_not_a_retyped_mapping` 对同样的输入
  （只有 sample、review 表为空）断言 `not visual.sufficient`。

两条在本轮 §5 下不可能同时绿。裁决：**按 §5 实施**，#2 转绿，#1 停在第一个断言（第 62 行）；
#1 的真正目标（v1 证据交给 v2 规则集不得 high）由新增用例以「有 canonical review」为前提完整覆盖，
另加「版本标签相同但规则内容不同（指纹不同）」一例。

CLOSURE_R3 必须写明这一点，并把 #1 记为「断言了门禁之前行为的过期反例」——不得记为已修、已通过，
也不得为了让 #1 绿而退回「store 读不到 review 就相信传入字典」。

### 10.2 §3 选择表按实测改写（2026-11-17，lane C 的 probe_read_modes）

契约初稿写了两处未经实测的推断，lane C 的真 SQLite 探针把它们推翻了，按实测改写：

1. 「WAL、两者都不在（已 checkpoint）→ `ro` 零副作用」**不成立**：实测直接 `ro` 会新建 `-wal`(0 B) 与 `-shm`(32 KiB)。改为私有副本。
2. 「WAL、`-wal` 在、`-shm` 不在 → 严格拒绝，仅显式 opt-in 才复制」改为**默认复制**，理由同上并见 §3 新增条目。
3. `ReadPlan` 增补 `sql_mode`、`promise`、`copy.source_members`、`evidence.changed`；`strict_no_side_effects` 只在 `names_and_bytes` 与 `copy_verified` 时为 true——活 WAL 的 `-shm` 锁字节会变，只能承诺 `names_only`。
4. 允许修改 `tests/test_v2_coverage_facts.py` 里两条编码旧语义的断言（`test_the_read_only_mode_follows_the_evidence_on_disk`、`test_a_file_that_changes_between_the_two_stamps_is_refused`），但必须换成更强的断言：活写者 → `ro`、无旁车 → `copy`、`immutable` 只能由调用方显式声明；`database_changed_during_read` 只属于显式 `caller_owned_snapshot` 的 immutable 读，并新增一条对照证明锁定读期间写者并发提交**不**被报这个错。其余断言一条不动。

