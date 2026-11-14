# Tk 初始化间歇故障：实测诊断（2026-11-15）

对象：`tk.Tk()` 在 pytest 里间歇失败，报 `_tkinter.TclError: Can't find a usable tk.tcl`，而那个文件确实存在。
本文件只记录**实测**与**判断分类**；没有改产品代码、没有改测试、没有装包、没有联网。
复现脚本全部在 `.runtime/jpgpng_u1u2_fix_20261115_01/tk_probe/`（仓库树外）。

## 1. 复现命令

````text
# 形状探针（纯脚本，不进 pytest）
$env:PROBE_REPO=<worktree>
python tk_probe/probe_a.py plain 40      # 40 次 Tk()+withdraw+destroy
python tk_probe/probe_a.py view 40       # 40 次 Tk()+CollectionView+dispose+destroy
python tk_probe/probe_b.py cwd 25        # 每次先 chdir 到临时目录
python tk_probe/probe_b.py two 25        # 两个 root 同时存活
python tk_probe/probe_b.py after 25      # destroy 前挂一个待触发的 after

# pytest 形状（仓库树外的最小复现）
$env:PYTHONPATH=<worktree>
python -m pytest tk_probe/test_tkprobe.py -q -p no:cacheprovider         # 默认捕获
python -m pytest tk_probe/test_tkprobe.py -q -p no:cacheprovider -s      # 关闭捕获
python -m pytest tk_probe/test_tkprobe_sleep.py -q -p no:cacheprovider   # Tk() 前 sleep 50ms
python -m pytest tk_probe/test_tkprobe_session.py -q -p no:cacheprovider # 全会话共用一个 root
python -m pytest tk_probe/test_tkprobe_diag.py -q -p no:cacheprovider    # 失败瞬间落盘诊断
````

`test_tkprobe_diag.py` 在 `tk.Tk()` 抛错的那一刻，用 Python 自己 `os.path.exists` / `open()` 读同一个文件，并把结果写进 `tk_probe/diag.json`。

## 2. 实测统计

| 条件 | 运行次数 | 失败次数 | 失败率 |
| --- | --- | --- | --- |
| pytest，默认捕获，每个用例建/销 root | 28 | 8 | 29% |
| pytest，默认捕获，诊断夹具（每轮 4 用例） | 12 | 5 | 42% |
| **pytest，`-s`（关闭捕获），每个用例建/销 root** | **28** | **0** | **0%** |
| pytest，默认捕获 + `tk.Tk()` 前 `time.sleep(0.05)` | 12 | 0 | 0% |
| **pytest，默认捕获 + 全会话共用一个 root** | **12** | **0** | **0%** |
| pytest，`-s` + 共用一个 root | 6 | 0 | 0% |
| pytest，默认捕获 + 显式设 `TCL_LIBRARY`/`TK_LIBRARY` | 12 | 2 | 17% |
| 纯脚本，40 次 `Tk()` 反复建销 | 40 | 0 | 0% |
| 纯脚本，40 次 `Tk()+CollectionView+dispose` | 40 | 0 | 0% |
| 纯脚本，chdir / 两个 root 同时活 / 待触发 after（各 25） | 75 | 0 | 0% |

另一条旧观测：真实模块 `tests/test_collection_presentation.py` 连跑 20 次，**8 次有 1 个 setup error**（同量级）。

## 3. 失败瞬间的磁盘证据（`tk_probe/diag.json`）

失败时同一个进程、同一时刻：

- `D:/Miniconda3/envs/JuiLiu/Library/lib/tk8.6/tk.tcl`：`exists=true`、`readable=true`、头 4 字节 `2320746b`（就是 `# tk`）；
- 反斜杠写法同样可读；
- `cwd` 正常（工作树根），`TCL_LIBRARY`/`TK_LIBRARY` 都是 `null`；
- Tcl 报的搜索列表**开头是** `.../tcl8.6/tk8.6`（把 tk 目录拼在 tcl 目录底下，是错的），而不是正常那组 `.../tcl8.6` + `.../tk8.6`。

也就是说：**Tcl 说读不到的那个路径，Python 在同一瞬间能读**；而 Tcl 的搜索基址退化了。

## 4. 判断

### 已证实

1. **触发者是 pytest 的输出捕获**，不是我们的控件代码：同一份最小夹具，捕获开 8/28 失败，关 0/28；把同一份夹具放到仓库树外结果一样，说明与具体测试文件无关。
2. **不是 root 数量、不是 destroy 时机、不是 cwd、不是待触发的 `after`**：纯脚本里这些形状各 25–40 次全绿。
3. **不是磁盘/权限/杀毒层面的瞬时读失败**：失败瞬间 Python 读同一文件成功。
4. **不是缺环境变量**：显式设 `TCL_LIBRARY`/`TK_LIBRARY` 之后仍然 2/12 失败。
5. **失败发生在"上一个解释器已销毁、下一个还没建起来"的窗口**：单会话单 root 时 0/18；每次建销时才有。
6. **加 50 ms 延迟就消失**（0/12）：说明它有明确的时间/竞争特征，而不是配置错误。

### 未证实（不要当结论用）

1. **具体是 Tcl 的哪一块进程级状态坏了**。最像的是"库基址这类进程级缓存在解释器 `Tcl_Finalize` 之后失效或被复用"，证据是失败时搜索列表退化成 `$tcl_library/tk8.6`；但没有源码级证据，也没有在 Tcl 内部打点。
2. **为什么捕获会放大它**：捕获多出临时文件、fd 复制与更多输出写入，可能只是改变了内存/时序布局。"捕获导致"还是"捕获加剧"没有分开证明。
3. **是不是本机独有**：只在当前这台 Windows + 这个 conda 环境上测过，没有换卷、换机器、换 Tcl 版本对照。

### 建议但未验证

1. **验收与 CI 里给三个 Tk 模块加 `-s`**（关闭捕获）。实测 0/28。代价：真失败时看不到捕获输出，需要复现时去掉 `-s` 再跑一次。改动只在验收驱动一行（`STEPS["tk_view"] = ([...], ["-s"])`），不碰产品代码。
2. **把这两个夹具改成会话级单 root**（每个用例只建视图、不建解释器）。实测 0/18。代价：用例之间共享 Tk 状态，可能互相污染；要逐条确认现有断言不依赖"干净解释器"，这件事没做，属于产品测试所有者的决定。
3. 两者可叠加，但**不建议只靠 `-s` 掩盖产品侧的 Tk 生命周期问题**：如果将来界面上有"关窗再开"的真实路径，同样的窗口会在用户那里出现。建议另开稳定性任务，用单 root 或显式终止策略把那条路径测出来。
4. 不建议：加 `pytest-rerunfailures`（要装包，且会把真失败也吞掉）；或把 Tk 测试标 skip。

## 5. 与既有记录的关系

- 上轮把 Tk 失败记成"环境 flake，重试一次通常就好"——本文件确认了这一点，并给出**可量化的触发条件**（捕获开 29% vs 关 0%）与**两个可验证的缓解措施**。
- 本文件不改产品代码、不改测试；两条建议的实施与验收由主线程决定。
