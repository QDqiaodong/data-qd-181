# 离线可复算发布包（astro-release-package/v1）

`astro_stack.release` 与 `astro-release` 命令在既有 astro-stack、astro-archive、
discovery、sky 入口之外新增本地发布工具，不改变任何既有契约。仅使用 Python 标准库，
不访问网络、不做加密或签名。工具只服务使用现有逐帧校准的**经典档案**
（archive definition 的 `products` 为 `null`）；campaign 定义被明确拒绝，不新增背景拟合。

## 公开接口与命令

- `issue(library_path, archive_path, request, license, fault_hook=None)`：
  读取发布库路径、源校准档案路径、请求 JSON 与许可 JSON，返回 JSON 回执
  `{request_id, dataset_id, revision, sha256}`。`request`/`license` 可传已解析对象或
  严格 JSON 文件路径。发布库不存在时按 `astro-release-library/v1` 布局创建。
- `export(library_path, request_id, output_path)`：按请求号取回一份已签发包，写入
  **尚不存在**的路径；导出字节的 SHA-256 必须与回执一致，否则报错并删除半成品。
- `verify(package_path, expected_sha256)`：只读包与接收者单独保存的可信摘要，先核摘要，
  再从证据离线复算；不读取源档案、许可或任何其他文件。返回 JSON 复算结果。
- 命令行三个动作：
  - `astro-release issue --library L --archive A --request R.json --license K.json`
  - `astro-release export --library L --request-id ID --output P.json`
  - `astro-release verify --package P.json --expected-sha256 HEX`
  成功打印 JSON、退出 0；输入、冲突、损坏包或文件操作失败退出 2，且不留下冒充成功的导出。

业务/输入/包完整性错误抛 `ValueError`（含 `ReleaseError`、`PackageError`），文件错误抛
`OSError`。

## 请求与许可（严格 JSON）

请求恰含 `request_id`、`dataset_id`、`revision`、`region`；region 恰含
`x0`、`y0`、`x1`、`y1`，零起点、半开 `[x0,x1) × [y0,y1)`，必须非空且完全落在档案画幅内。
`revision` 为非负整数且必须是**精确已提交修订**，不回退最新、不自动向前；`dataset_id`
必须与源档案一致。许可恰含 `dataset_id` 与 `denied`；每条 denied 恰含
`frame_id`、`frame_version`（正整数）、`x`、`y`（探测器坐标，必须在探测器范围内），
身份 `(frame_id, frame_version, x, y)` 不得重复，最多 50,000 条。许可可包含该修订已不使用
的旧帧版本或之后的版本——只按身份精确匹配，不回查档案历史。每份新交付读取一份完整许可，
替换许可只影响后续新请求。

两个文件沿用项目严格 JSON 约定：8 MB 输入上限、重复键拒绝、非有限常量拒绝、未知/缺失字段
拒绝、布尔不充当整数、ID 为非空且不超过 200 字符的精确字符串；所有整数上限不超过 10^12。

## 格状态与最小证据

先按档案修订与逐帧绑定（release 选择、暗场、平场、屏蔽、饱和、整数注册）确定矩形内的真实
贡献，再套用许可。每个输出格恰有一种结果：

- `uncovered`：原叠加在该格无任何有效贡献。不含样本，曝光与计数为 0，速率/方差为 `null`。
- `withheld`：至少一条真实有效贡献命中 denied。只交付状态；不交付速率、方差、曝光、计数，
  不交付该格任何贡献样本，也不点名阻断身份。被掩膜、饱和、零平场或落到矩形外的受限样本不会
  阻断其他格。即使删去受限贡献后速率恰好不变，也不把子集重算冒充原修订结果。
- `published`：其余格。交付原叠加的有理数速率、方差、有效曝光与贡献计数。

整个矩形全部不可发布仍是成功包。published 格的每条贡献凭据足以离线确定：原始帧版本与探测
器坐标、原始计数及其方差、帧曝光、帧平移、实际绑定的 release 与暗场、该暗场样本的原计数/
方差/曝光，以及该探测器位置的平场归一系数。平场样本与暗场样本只包含被 published 格引用的
位置；`mean_flat` 是档案中该平场**完整有效矩阵**的归一因子，禁止用发布矩形内的平场重算。
共享表可以规范化，但不允许留下任何未被 published 格引用的样本或校准记录；withheld/uncovered
格不携带任何像素证据，包内不记原档案路径、不含矩形外像素、不复制整帧/整暗场/其他修订/完整
许可表。

## 包格式与确定性排序

包是单个 canonical JSON 文档：对象键按 Unicode 码点排序、数组顺序固定、紧凑分隔符、
`ensure_ascii=False`、无墙钟时间、无目录与输入排版。格式标识
`astro-release-package/v1`，顶层恰含 `format`、`request`、`frame`、`cells`、`region`、
`frames`、`releases`、`darks`、`flats`、`contributions`。

- `frame` 给出原画幅 `width`/`height`，供离线坐标检查。
- `cells` 按绝对坐标行优先排列，恰好覆盖请求矩形。
- `frames`/`releases`/`darks`/`flats` 按各自 ID 升序；表内样本按 `(y, x)` 升序。
- `contributions` 按 `(frame_id, output_y, output_x, detector_y, detector_x)` 升序，
  以表索引+样本索引引用共享表；published 格的贡献索引升序且唯一，每条贡献只属于一个格。
- 有理数以 `Fraction` 约分字符串（`n` 或 `n/d`）输出；无 published 格时区域
  `rate_sum`/`variance` 为 `null`。

给定同一请求与相同有效事实，包字节完全确定；回执中的 `sha256` 是包文件原始字节的
SHA-256。

## 区域总和与误差身份

另报矩形内 published 格的速率总和（格速率直接相加，不乘曝光）与区域方差，并给三种状态计数。
区域方差先汇总各格帧像素独立项；暗场误差身份是 `(dark_id, detector_x, detector_y)`：同一暗场
id 的同一探测器位置，即使经不同帧、不同 release 或不同平移映射到不同格，仍是同一误差源，
其在区域上的系数为各格系数之和，按 `variance * (Σg)^2` 计入跨格相关性；不同暗场 id 即使取值
完全相等也不得合并。

## 离线 verify 的职责边界

验收边界是打包器相对可信档案与许可的取数正确。验证器先检查可信摘要（包内任何自报摘要都不能
取代它），随后独立重做坐标映射、标定、有效曝光加权与精确有理数误差传播，逐格及区域总和与包
内值一致才算成功，而不是只核摘要或复述包内结果。验证器明确拒绝：证据重复使用、同身份事实互相
矛盾（同一帧版本的曝光/平移在多处不同、同一暗场样本有两个方差等）、缺项、坐标越界、与注册
偏移不符、未绑定到 published 格的冗余表记录/样本、withheld 格附带证据、非约分有理数字符串、
未知格式与重复 JSON 键。验证器不知道原目录，也不隐式读取许可或源档案，因此不负责证明原档案
是否还有其他曝光或某格为何被 withheld。

## 失联重取、原子性与并发

同一发布库内 `request_id` 是跨数据集唯一的发放身份。先解析请求再查已签发记录：

- 同号同规范请求（按字段值比较，JSON 排版与键顺序不算变化）永久返回同一回执，不检查源档案与
  许可当前是否可读或已变更。
- 同号不同请求报冲突；许可字段不参与同号重试的请求比较。
- 未签发的号才读取指定修订与本次许可；任何输入或计算失败都不占号。

发布库布局：`state.json`（格式与签发台账）、`packages/<sha256(request_id)>.json`（不可变规范
包）、`.lock`（跨进程文件锁）。源档案在锁内只做固定修订快照捕获，科学计算与打包在档案锁外
完成，允许档案在期间继续提交。单条签发的提交边界是 `state.json` 的原子替换：包先以最终文件名
就位，台账随后切换；重开只能看到“尚未签发”或“一份完整可取回的签发”。重开会清理未被台账引用
的暂存包，但对台账已引用而包缺失或摘要不符的损坏明确报错，绝不按新许可悄悄重建。并发提交同号
时，同规范请求返回同一份签发，不同请求至多一个成功，其余冲突失败。`export` 复用 create-only
语义，绝不覆盖已有目标。
