# 归档采集器（维护工具，非产品代码）

`resume_archive.py` 是**维护工具**：它按一份已批准的归档计划搬动历史批次，并记录**它实际测量到的**内容与身份。它：

- **不在产品启动路径上**，也不被 `启动.bat`/`maintain.cmd` 调用；
- **不 import 产品代码**（`core/**`、`gui/**`、`utils/**` 一行都不依赖），可以单独复制到维护仓；
- 不联网、不装依赖、不删除历史批次、不覆盖已有目标文件。

它最初只存在于工作区 `.runtime/workspace_tidy_20261114_01/resume_archive.py`（不受版本管理）。本目录是**纳入版本管理的修订版**；旧脚本原样保留、不改、不再执行。

## 身份三分量（契约 v6 AMD2 第 6 节）

`file_record()` 把一个文件的身份拆成三件事，再组合：

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `handle_identity` | 哈希读的就是这个句柄，且哈希前后句柄没换对象 | `ok` / `unknown` / `changed` |
| `path_binding` | 读完时这个路径仍指向那个对象 | `ok` / `unknown` / `changed` |
| `content` | 读取期间大小与 mtime 没变 | `ok` / `changed` |
| `identity` | 三者组合 | `ok` / `unknown` / `changed` |

组合优先级（从高到低，见 `identity_components()`）：

1. `content` 变了 → `identity=changed`（reason `size_changed_during_hash` / `mtime_changed_during_hash`）；
2. 句柄前后都有效但不一致 → `handle_identity=changed`、`identity=changed`（**即使路径未知**）；
3. 句柄任一未知 → `handle_identity=unknown`、`path_binding=unknown`、`identity=unknown`；
4. 句柄正常但最终 `lstat` 未知 → `path_binding=unknown`、`identity=unknown`（**这条是复审反例**：好句柄不能把读不到的路径补成"已证明"）；
5. 句柄与路径都有效但不一致 → `path_binding=changed`、`identity=changed`；
6. 三者都有效且一致 → `identity=ok`。

**已知冲突优先于 unknown**：读完时的句柄与路径两侧都可测且不一致 → `path_binding=changed`，即使第一次句柄测量读不到也写 `identity=changed` 并保留 `path_replaced_during_hash`。其余情况下句柄不干净（任一未知或已变）时 `path_binding` 仍记 `unknown`——名字没有被一个可担保的句柄证明；`path_binding=ok` 只出现在句柄全程有效且名字对得上的时候。

**`None` 不同时表示"匹配"和"无证据"**：未知一律是显式字符串 + `identity_reason`。收据里 `identity_state` 只有两侧都带上有效身份且一致时才是 `proven`，否则 `unproven` 并计数。

Windows 上目录枚举缓存（`DirEntry.stat`）的 `st_dev/st_ino/st_nlink` 是 0，而 `os.stat`/`os.fstat` 给出真实值；所以身份只从**打开后的句柄**取，并用 `os.lstat` 复核路径。

## 运行

```powershell
$py = "D:\Miniconda3\envs\JuiLiu\python.exe"

# 只读盘点：不移动任何东西，产出 survey.json
& $py -s -B -X utf8 resume_archive.py survey --plan <计划目录> --run <空的记录目录> \
    --workspace <工作区根> --archive <归档根> --repo <产品仓库>

# 执行已批准的批次（会移动文件；本仓库从未在真实批次上跑过）
& $py -s -B -X utf8 resume_archive.py apply --plan <计划目录> --run <同一个记录目录>
```

`--plan` 目录必须包含 `archive_plan.json`（必需）与 `project_local_plan.json`（可选）。没有默认真实批次：不给 `--plan` 时工具只会在 import 后报"plan 目录没有 archive_plan.json"。

## 测试

```powershell
& $py -s -B -X utf8 -m pytest test_identity_capability.py -q -p no:cacheprovider --basetemp=<新建目录>
# 或者，不需要 pytest：
& $py -s -B -X utf8 test_identity_capability.py
```

覆盖：全有效、硬链接、独立副本、路径替换（同内容同长度）、内容变化、junction 不穿越、句柄全 0、仅第一次 `fstat` 未知、仅第二次未知、**仅最终 `lstat` 未知**、已知句柄变化+路径未知、跨卷身份变化（合成分类）、哈希后出现重解析点、未知库存、整树改名、无摘要不判等、缺文件、`apply()` 合成回执（`proven` / `unproven`）、计划目录必需。每个用例先断言注入路径都在本用例临时树内。

## 与复审回归的关系

复审的 14 条反例里有一条针对本工具（`test_pa03_missing_path_identity_cannot_be_reported_proven`）。它通过环境变量指向本文件：

```powershell
$env:PIXIVKEEP_ARCHIVE_COLLECTOR = "<repo>/tools/maintenance/resume_archive.py"
```

不设置时该用例显式 `skip`（公开仓库里没有这个脚本的历史原因），**不得**为了变绿改断言。

## 明确不做 / 未证明

- 本轮**没有**在任何真实归档批次上跑 `survey` 或 `apply`；`apply()` 只在合成批次上端到端验证过。
- **跨卷移动未实测**：本机单卷。跨卷会改变卷身份，分类结果是 `changed`，但"真实跨卷后仍能证明身份"没有证据。
- **旧 600,447 个文件的搬迁前身份基线补不回来**：那批 `before` 清单里 `file_id`/`hardlink_count` 全是 0，只能更正结论，不能追溯造证据。修好采集器也不改变这一点。
- 不重跑旧搬迁、不重建旧的 428 个合成链接、不改时间戳、不改写旧清单或历史批次记录。
- 收据只说"这次移动前后我测到了什么"，不是签名，也不是"以后永不变"的承诺。
