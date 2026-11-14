# U2-B 五路拆分（筛选解释、离线重评估、方案快照）

基线：U2-A 收口之后。**只读**：不改候选、不删任何文件、不执行用户表达式、不发起 HTTP。

## 0. 既有事实（已核对入口）

| 来源 | 能给出 |
| --- | --- |
| `core/candidate_filter.py` `evaluate_filters(FilterSpec, bookmarks, ai_flag)` | `filtered` / `metadata_unknown` / `allow`，原因码 `bookmarks_below_minimum` `bookmarks_unknown` `ai_excluded` `ai_flag_unknown` |
| 同上 `ArtworkMetadata` | `illust_id` `kind`(illust/manga/ugoira/unknown) `bookmarks` `ai_flag` `available` `page_count` `original_urls` |
| 同上 `make_candidate` | `artwork_unavailable` `ugoira_unsupported` `original_metadata_unknown` `invalid_original_metadata` |
| `core/task_contracts.py` `FilterSpec` | 冻结条件对象：`min_bookmarks`(int|None)、`exclude_ai`(bool)，已校验 |
| 同上 `OutputSpec` / `QualitySpec` | 方案快照的输出与完整性两部分，都是冻结 dataclass |

**关键结论：不要重写判定。** `explain()` 必须复用 `evaluate_filters` 的结论，把它的原因码映射进新的封闭码表；这张映射表本身就是被测对象。已知拒绝优先于未知（既有语义），不得反转。

## 1. 冻结契约

五路文件互斥，各自只新增自己的文件；跨路只靠本文档冻结的字典形状，不互相 import（否则五路无法并发）。

```text
B1 core/filter_reasons.py
   REASON_CODES = ("favorites_below", "ai_excluded", "type_unsupported", "field_unknown", "access_denied")
   FIELDS       = ("bookmarks", "ai_flag", "type", "availability", "original_urls")
   reason(code, field, evidence, *, unknown=False) -> {"code", "field", "evidence", "unknown"}
   validate_reason(record) -> None      # 闭集校验：码不在表内 / 字段不在表内 / evidence 不是 JSON 标量 → ValueError
   LEGACY_CODE_MAP = {
     "bookmarks_below_minimum":  ("favorites_below",  "bookmarks",     False),
     "bookmarks_unknown":        ("field_unknown",    "bookmarks",     True),
     "ai_excluded":              ("ai_excluded",      "ai_flag",       False),
     "ai_flag_unknown":          ("field_unknown",    "ai_flag",       True),
     "ugoira_unsupported":       ("type_unsupported", "type",          False),
     "artwork_unavailable":      ("access_denied",    "availability",  False),
     "original_metadata_unknown":("field_unknown",    "original_urls", True),
     "invalid_original_metadata":("field_unknown",    "original_urls", True),
   }
   映射表缺项即 ValueError（不许自由字符串透传）。

B2 core/filter_explain.py
   explain(candidate, conditions) -> {"verdict": "match"|"exclude"|"unknown",
                                     "reasons": [{code, field, evidence, unknown}]}
   - candidate：ArtworkMetadata 或同形 Mapping（鸭子类型，只读字段，不改它）
   - conditions：FilterSpec 或同形 Mapping（只认 min_bookmarks / exclude_ai）
   - verdict 规则：任一 unknown=False 的原因 → exclude；否则任一 unknown=True → unknown；否则 match
   - 三态映射（写进 docstring）：match↔allow、exclude↔filtered、unknown↔metadata_unknown
   - type_unsupported 与 field_unknown 不混：kind=="ugoira" → type_unsupported；kind=="unknown" → field_unknown("type")
   - available 只在 is False 时成为 access_denied；available is None 不产生原因（与 make_candidate 一致，刻意）
   - 零 I/O：不 HTTP、不读文件、不写库

B3 core/filter_reevaluate.py
   reevaluate(candidates, old_conditions, new_conditions) -> {
     "kept", "newly_excluded", "newly_included", "still_excluded", "uncovered",
     "uncovered_reasons"}                 # 五个桶的值是 illust_id 列表
   - 只读：不删文件、不标记下载、不写库；candidates 里的文件一件都不动
   - 五桶穷尽且互斥：len(kept)+len(newly_excluded)+len(newly_included)+len(still_excluded)+len(uncovered) == len(candidates)
   - 放宽（new ⊃ old）时旧候选进 newly_included；收紧时进 newly_excluded / still_excluded，绝不删除

B4 core/filter_plan.py
   @dataclass(frozen=True, slots=True) FilterPlan:
     conditions: FilterSpec; output_policy: OutputSpec; integrity_policy: QualitySpec; resource_start: Mapping
   - fingerprint() -> sha256(canonical_json(as_dict()))，与字典顺序无关
   - with_conditions(new_conditions) -> 新 FilterPlan（旧对象字段一个都不变）
   - resource_start 只接受 JSON 标量（str/int/bool/None），深冻结；默认 {}
   - as_dict() / from_dict() 往返稳定；部件不是对应冻结类型即 ValueError

B5 tests/filter_fixtures.py + tests/test_filter_fixtures.py
   合成候选夹具（不建库、不读真实数据），至少九类：
   收藏数低于阈值 / AI 命中排除 / ugoira / kind=unknown / bookmarks=None /
   ai_flag=None / available=False / 全字段齐（match）/ conditions 全空（无筛选）
   每份给出 (candidate, conditions, expected_verdict, expected_codes)。
   AST 断言不 import core（不依赖并行开发中的 B1-B4）。
```

## 2. 主线程合流后做

- `tests/test_filter_matrix.py`：把 B5 的九类夹具喂给 B2，逐条断言 verdict 与 code；断言 B2 发出的码与字段都在 B1 的闭集内；B3 五桶穷尽；B4 快照不可变。
- `docs/optimization/2026-11-14/U2B_FILTER_EXPLAIN.md`：口径说明 + 三态映射表 + 一个示例（示例候选必须标明是示例）。
- GUI 接线**不在本轮**：U2-A 与 U2-B 全部合流后再统一做（避免两条 lane 抢视图文件）。

## 3. 与冻结计划的差异（评审时看这一条）

`U2AB_PLAN.md` §2.1 写的 `reevaluate` 返回四个桶（kept / newly_excluded / newly_included / uncovered）。
本拆分改成**五个桶**，多一个 `still_excluded`：原四桶对"旧排除、新仍排除"没有任何归属，导致
"各桶加起来等于候选总数"这条不变量无法成立；而覆盖类交付物的核心正是这条不变量。多出的桶不改变任何行为
（仍然一件文件都不动），只是把原本丢失的那部分说清楚。

## 4. 完成定义

五路测试全绿；合流后矩阵覆盖九类合成候选；放宽 → `newly_included` 非空且无删除；收紧 → 无删除且
`still_excluded` 承接旧排除；方案快照改了不影响旧对象；全量矩阵与三个 Tk 模块不回归。
