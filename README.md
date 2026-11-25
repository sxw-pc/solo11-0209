# 湖区浮标年度回收与校对编排台

离线演示一个年度批次的漂移取证、船班预约、上岸换件、拆件保管和复投后的数据连续资格。坐标为虚构格点，时间为同一批次内的整数分钟。每个浮标每轮回收一次，分别更换一个电池件和一个传感件。需要 Python 3.11+ 和 SQLite 3.37+。在本目录直接运行。

```powershell
python -B -m buoy_recovery --help
python -B -m buoy_recovery --db lake.sqlite demo
python -B -m buoy_recovery --db lake.sqlite --at 67 plan
python -B -m buoy_recovery --db lake.sqlite audit
python -B -m unittest discover -s tests -v
```

`load FILE` 采入形如 `[["事件号","动作",参数…], …]` 的 UTF-8 JSON。`demo` 可在同一库重跑。`plan`、`audit` 以只读方式打开库；`audit` 按已提交顺序重放到内存 SQLite，逐表比较并输出重算业务状态。事件号异内容或任一操作失败均使整批回滚。

| 动作 | 事件号和动作名之后的参数 |
|---|---|
| `buoy` | 浮标、目标 x、目标 y、要求的维护技能 |
| `fix` | 浮标、观测来源、参考分钟、x、y、误差（1–8 格） |
| `packet-batch` | 拼校批次号、可选定长字节数（缺省 5） |
| `packet-catalog` | 批次号、`[原件号,浮标,观测分钟,来源], …` 固定报头目录（含未收到负载的原件） |
| `packet-original` | 批次号、原件号、实际收到的定长负载（十六进制） |
| `packet-retransmit` | 批次号、原件号、一次补传的一个原件负载（十六进制） |
| `packet-parity` | 独立证据号、批次号、同批原件号组、逐位异或负载（十六进制） |
| `dock` | 开放窗号、泊位、开放分钟、关闭分钟 |
| `trip` | 船班、开放窗号、船号、出发、归航、覆盖中心 x、中心 y、覆盖半径、维护技能、名额 |
| `reserve` / `cancel` | `reserve` 为浮标、船班；`cancel` 为浮标 |
| `lift` / `land` | 浮标、动作分钟 |
| `equipment` | 部件号、`battery` 或 `sensor`、已装浮标或 `null`（备件） |
| `swap` | 浮标、旧件号、新件号、动作分钟 |
| `transfer` | 拆件、交接分钟、交方、接方 |
| `pair` | 浮标、传感件号、参考分钟、设备分钟 |
| `limits` | 浮标、正整数版本、复投范围半径、允许的最大绝对时钟偏移 |
| `redeploy` | 浮标、复投参考分钟 |

- `position.py` 使用评价时点之前最多 20 分钟的观测，每来源取最新一条。至少两个来源，来源最新分钟相差最多 10，且任意两条误差菱形相交，才能输出可信包络。包络以最新报告中心为锚，覆盖所有入选误差并按最旧报告每满五分钟增加一格。`--at` 可观察指定分钟的置信类别及包络。
- 丢负载的浮标定位包由拼校批次采录：`packet-catalog` 登记每个原件号的固定报头（浮标、观测分钟、来源），负载统一为 5 字节 `>hhB`（有符号 int16 x、有符号 int16 y、误差字节 1–8）。`packet-original`/`packet-retransmit` 采录实际收到的原件，`packet-parity` 采录带独立证据号的逐位异或包。拼校成员必须全部属于同一批次。同原件号同内容重传只留一份，异内容写 `original-reprint` 冲突；同证据号拼校包异内容写 `parity-reprint` 冲突。原始负载在 `packet_originals`、联立推导负载在 `packet_derived`（含 `derived/withdrawn` 状态与证人列表）分别可查。
- 联立恢复按批次对 GF(2) 消元（Python 大整数同时处理全部字节位，不枚举候选字节串）：已知原件代入右端，缺失原件为未知量。只有某原件在所有可行分配下逐字节唯一（RREF 主元行不含自由变量）、且解码通过 `fixes` 字段检查时，才作为 `derived` 支撑写入现有 `fixes` 取证流程；其余继续待收。推导负载的 `witnesses` 只能引用实际收到的原件号或拼校证据号。消元出现 `0 ≠ 0` 行即无可行分配，冲突行 `packet_conflicts.refs` 给出可回查的拼校证据号和/或原件号；该批所有推导立即从 `fixes` 撤回（原始原件保留），`fleet` 因而不再放行船班/起吊，错误信息附带冲突编号，已完成的 reserve/lift/land 事实不回滚。`fix_support` 记录每条 `fixes` 行的原始/推导支撑与撤回状态，供 audit 重放比对。
- `fleet.py` 检查船和泊位在半开保留区间内各自唯一，船班往返须在泊位开放窗内。船班选项要求剩余名额、相同维护技能、完整包络落入覆盖，起吊时按当时观测复核。起吊后不能取消，归岸须在起吊后且于归航截止前完成。
- `custody.py` 在归岸后原子更换同类备件，旧件由船上暂存交给保管柜；交接记录需两位不同人员及有效时间。
- `continuity.py` 单独保留实物复投事实。连续资格只用复投分钟至随后第十分钟（含）的定位报告，以窗内最新观测分钟建立包络，且包络必须完全入目标范围。两件拆件都须在复投时已有效入柜。配时按同一浮标的传感件及挂载期核验，旧件取起吊前 15 分钟内，新件取最后一次更换后且不晚于复投的最新样本。参考时刻相距最多 45 分钟，偏移差最多 2 分钟，各自绝对偏移也须合格。晚到的窗内证据可改变数据资格，历史起吊、归岸和复投保留。
- `limits` 可在复投前独立设定浮标的目标半径和绝对偏移限值，缺省为八格和五分钟。版本取该浮标最高值，较早版本留档，同版本由不同事件重报触发冲突。复投锁定该浮标的配置，其他浮标仍可变更。限值只管复投后的数据资格，船班仍按各自声明的空间覆盖和技能运行。

`fixtures/demo.json` 让 `alpha` 占用 `wide` 唯一名额并在复投后获得 `continuous`，`beta` 因缺可执行班次等待。对演示库采入 `fixtures/rejected.json` 会因同泊位窗冲突回滚。采入 `fixtures/late-fix.json` 会使 `alpha` 转为 `hold-position`，而 `--at 67` 仍可单独查询该分钟的定位。`fixtures/drift.json` 用于构造较早的起吊改派反例。

丢负载拼校验收从 `demo.json` 放到 `t3` 并省略 `a1`、`b1`，再采 `fixtures/recovery-accept.json`（同批目录、其它负载与拼校包；补传前 alpha、beta 均无班次）。随后二选一采入 `fixtures/recovery-request-a1.json`（内容等于原 `a1` 负载，放行 alpha 的 `narrow`，beta 仍待证）或 `fixtures/recovery-request-b1.json`（等于原 `b1`，放行 beta 的 `wide`，alpha 仍待证），接收中心每次只补一个原件。`fixtures/recovery-lock.json` 给出一组无需补传、经多式联立唯一恢复 `lA` 的证据；`fixtures/recovery-lock-conflict.json` 随后制造与既有推导矛盾的拼校包，使该批推导撤回、起吊被拒，而已完成动作仍可查。`tests/test_recovery.py::SparseParityRegressionTest` 另含 36 个未收到定长负载、8 条线性独立拼校约束、其中 2 条必须靠跨式 GF(2) 消元（而非单未知数剥壳或候选枚举）才能恢复的回归。所有拼校事件都进 journal，`audit` 重放并逐表（含 `packet_*`、`fix_support`）比对，原包与推导结论均可重验。
