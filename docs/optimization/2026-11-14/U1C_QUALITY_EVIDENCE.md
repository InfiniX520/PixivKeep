# U1-C 质量证据与深解码（2026-11-14）

基线：U1-B 收口后。五条 lane 合流，本文档记录**能说什么、不能说什么**，以及本机为什么只有 PNG 真解码。

## 1. 五个维度互相不能替代

| 维度 | 取值 | 能说明 | 不能说明 |
| --- | --- | --- | --- |
| source | declared_original / unknown | 这次用的是元数据声明的原图 | 平台没有处理过作者源文件 |
| transport | length_matched / unknown | 实际字节数与记录一致 | 服务器返回的就是目标作品 |
| structure | ok / failed / undecided / unsupported | 容器与数据结构自洽（现有 `inspect_image`） | 像素能被解码 |
| decode | ok / failed / undecided / capability_unavailable | 指定解码器在限制内读完了像素流 | 远端原件真伪 |
| pages | all_frames / first_frame_only / unknown | 页/帧集合是否读完 | 多帧格式的每一帧都被语义校验 |

合成一行：**没有解码就没有 pages=all_frames**。只有 `decode=ok` 时才会写 `all_frames`，否则一律 `unknown`（reason=`decoder_did_not_read_frames`）。

## 2. 本机实际做了什么

- **PNG：真解码**（标准库，无外部组件）：解析 IHDR/PLTE/IDAT/IEND（含 Adam7 七趟）、zlib 解压、逐行反滤波（None/Sub/Up/Average/Paeth）、每行 filter 合法、每趟字节数精确、CRC 校验、未知 critical chunk 拒绝、APNG（acTL）明确不支持。
- **JPEG / GIF / WebP：能力不可用**。本机没有 Pillow/numpy，且用户本轮要求不安装任何包。严格策略下 `decode=capability_unavailable`（reason=`no_decoder_component`），**绝不当 ok**。
- **资源上限**：`max_bytes / max_pixels / max_frames / max_seconds` + stop。超限或取消 → `undecided`（reason=`bytes_limit`/`pixels_limit`/`frames_limit`/`time_limit`/`cancel`），**不是 failed**。
- **只读证明**：`check_file()` 解码前后各算一次 sha256，报告里写 `unchanged` 与前后摘要。

## 3. CLI

```powershell
& $py -s -B -X utf8 maintain.py decode-check <文件> [<文件> ...]            # 默认要求解码
& $py -s -B -X utf8 maintain.py decode-check <文件> --structure-only      # 只做结构级
& $py -s -B -X utf8 maintain.py decode-check <文件> --max-pixels 5000000  # 限制
```

退出码：**0** 每个文件都真的解码通过；**2** 有文件被判定为内容不通过（结构或解码 failed）；**5** 没有全部解码通过（未请求 / 能力不可用 / 超限 / 取消）；**3** 文件读不出来；**4** 参数被拒。

## 4. 为什么没有接进 `scan`

计划里原本写"给 `maintain.py scan` 加 `--decode`"。实际没有这么做，理由：`scan` 的语义是"账本行 vs 磁盘文件"，它有**逐页预算与游标**；把解码塞进去会让同一行在一次扫描里既做哈希又做像素解码，预算含义和耗时都变了，且会让"没解码"与"解码失败"混进同一份 coverage。

所以先给独立的 `decode-check`（单/多文件、明确退出码），把"是否给扫描加解码预算"留到 U2-C 性能那一轮单独评估。这是**有意的范围收缩**，不是遗漏。

## 5. 验收结果

- `core/quality_evidence.py` 13 项、`core/png_decode.py` 24 项、`core/decode_limits.py` 48 项、`core/decoder_registry.py` 15 项、`tests/png_samples.py` 12 项；
- 跨模块矩阵 `tests/test_decode_matrix.py` 13 项；CLI `tests/test_decode_cli.py` 6 项；
- 真实文件冒烟：`docs/refactor/designs/approved_2026-10-21/follow_user.png`（117,640 B，1360×900）→ 结构 ok、解码 ok、1,224,000 像素、用时约 0.72 s、**解码前后 sha256 不变**。

## 6. 诚实列出的限制

1. **没有安装任何解码组件**：所以 JPEG/GIF/WebP 的"能解码"分支从未在本机跑过；只验证了"能力不可用"这一支。
2. C2 的隔行覆盖以 2×2/8×8 为主；16-bit + Adam7、tRNS/sBIT/iCCP 等辅助 chunk 只做"跳过 + CRC 校验"，不做语义校验。
3. `memory_limit` 分支只有代码路径，没有可复现测试（不想靠分配巨内存触发）。
4. 未测试从不可 seek 的管道读取（实现只要求 `read`）。
5. `pages` 维度只表达"读完/没读完"，不表达"每一帧都被语义校验"。
6. 历史 `verification="structure_checked"` 行**一个字都没改**：本批只新增维度，不批量升级旧收据。
7. 结构与解码的失败**不双报**：结构已经 failed 的文件不再解码（reason=`structure_not_ok`），避免"两个 failed"掩盖是谁先发现的。


## 7. 复现

```powershell
$env:PYTHONNOUSERSITE = '1'
& 'D:\Miniconda3\envs\JuiLiu\python.exe' -s -B -X utf8 -m pytest tests/test_decode_matrix.py tests/test_decode_cli.py -q
```

## 8. 对抗审计后的修复（2026-11-14）

U1-B/U1-C/U2-A/U2-B 四块交付做了一轮**只读对抗审计**（另起一个不带实现上下文的审计者，逐行对 diff 与契约）。U1-C 被抓到 1 个 P1、3 个 P2、4 个 P3，全部已修，修复都带"修复前会红"的回归测试。

### 8.1 P1：Adam7 隔行解码的跨趟 previous 未重置

PNG 规范要求**每一趟的第一行**把上一行视为全 0。原实现在换趟时只清当前行、不清 `previous`，于是：

- 七趟首行都用 Up 的合法 8×8 隔行图 → `IndexError: bytearray index out of range` 从 `decode_png` 逃出（当时只捕获 `_Halt` 与 `MemoryError`），CLI 只捕获 `OSError` → traceback + 退出码 1；
- 只让第 4 趟首行用 Up（行宽与前一趟相同、不越界）→ **`decode=ok` 但像素是错的**，还照常报 `rows_unfiltered=15`。

这条要命的不是崩溃，是**假 ok**：一份收据说"解码通过"，像素却不是原图。

修复与证据：

- 换趟时清空 `previous`，并在 `_finish` 加 `index < len(previous)` 上界检查（将来若有人再把 previous 弄短也不会越界）；
- 非 `_Halt` 异常收成 `failed`（`row_length_mismatch` 一类），`TypeError`（参数类型错）仍然上抛，因为那是调用方 bug；
- 两条新测试的期望像素由 `tests/png_samples.py` 的**前向编码器**独立算出（不拿被测实现当期望）；
- **红证据**：把这两条测试放回修复前的提交（`6ff8ec1`）上跑，实测 `IndexError: bytearray index out of range`（png_decode.py:183）与 `assert 'f77d26…' == '90c0cc…'`（state 仍是 ok），两条都红；修复后两条都绿且摘要一致。

写边界测试时还发现一个附带缺陷：**IEND 的 CRC 从来没被读过**，`bytes_read` 比文件少 4 字节，所以"在 IEND 数据之后被截断"的文件会被判 ok。已改为与其他 chunk 一样读取并校验 IEND CRC，并补两条测试（`bytes_read == len(image)`、缺失 IEND → truncated）。

### 8.2 P2：三套默认上限与一个没人用的 Budget

原状态：`core/decode_limits.Budget` 与 `DEFAULT_*` 在全仓**零个生产调用点**（48 项测试全测一个没人用的类），真正在跑的 `png_decode` 自己写了一份 `_Budget`，时钟是 `time.monotonic`（而 `decode_limits` 的注释专门解释过 Windows 上它有约 15.6 ms 粒度），边界是 `>` 而文档写 `>=`，默认字节上限 2 GiB（维护路径传的是 256 MiB）。

现在：`png_decode` 只用 `core.decode_limits.Budget` / `DecodeLimits`，时钟是 `perf_counter`（可注入），字节边界按 `DecodeLimits` 文档"恰好等于上限允许、超过才拒绝"，时间按"达到即超限"；`decode_png` 结果里多一个 `limits` 键记录本次实际生效的四个上限。两处默认值（库 64 MiB / 维护 256 MiB）现在是**两个显式命名对象**，差异由测试锁住。

### 8.3 P2：收据把"没测量"写成 0

`DecodeReceipt` 的 `frames/pixels/seconds` 现在允许 `None`，语义写进 docstring：**`None` 是没测量，`0` 是测量到 0，两者不许混**。非 PNG 路径（无解码器）三个字段全 `None`；PNG 路径只有 `state == ok` 才写真实帧数与像素，被预算或失败停下的解码写 `None`（`png_decode` 里的 `frames=0` 是占位、`pixels` 是头部尺寸，都不该冒充测量）。CLI 文本对 `None` 打印**「未知」**而不是 `None` 或 `0`。

### 8.4 P2/P3：其余三条

- `--max-bytes 0` 原来被 `x or default` 静默换成 256 MiB，负数直接 traceback；现在显式区分"没给"与"给了 0"，非法上限 → **退出码 4**（文档一直承诺、但从未产生过的那个码）。
- `getattr(evidence, "size", target.stat().st_size)` 的默认值会被无条件求值：文件在结构检查后消失时会抛 `OSError` 而不是走 `unreadable` 分支。改成"先取属性、缺了再 stat、stat 失败进 failed/unreadable"，PNG 的 `open` 也包进同一分支。
- `decoder_error:<类型>:<原文>` 把异常原文（可能含路径、换行）原样写进收据。改成只保留**类型名 + errno**（`decoder_error:OSError:errno-28`，上限 96 字符），解码器自报的 reason 也限长 120 并带可见 `:truncated` 标记。

### 8.5 这次审计值得记住的一条

八个修复里有五个的症状都是**"结论比事实更乐观"**（假 ok、0 冒充未知、`ok`/`done` 混进三态、`STATE_MAP` 死表、重复 id 让批次失败）。测试全绿并没有拦住它们，因为测试与实现出自同一个理解。**能拦住的是"用另一套理解重算期望值"**——这也是这批修复每条都要求写"修复前会红"的原因。
