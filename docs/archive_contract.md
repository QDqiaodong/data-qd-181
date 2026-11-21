# 本地校准档案（schema_version=3）

档案在既有 `service.run(data)` 与 `astro-stack --source/--output` 之外新增独立入口
`astro_stack.archive` 与 `astro-archive` 命令，不改变模式一/模式二的任何输入限制、
数值、返回结构与签名规则。仅使用 Python 标准库，不访问外部服务。

## 公开接口

- `initialize(path, definition) -> {"dataset_id", "revision": 0}`：只允许创建尚不存在
  的档案；初始化失败不会留下可被误认为成功的目录，已有档案不能覆盖。
- `apply_batch(path, batch) -> {"dataset_id", "batch_id", "revision"}`：原子提交一个
  批次，每个成功批次恰好增加一个修订。
- `render(path, revision) -> report`：渲染一个**已经提交的精确修订号**，不接受未来修订，
  不暗中改成最新修订；不发布文件。
- Python 接口以 `ValueError` 表达业务/输入/档案完整性错误（含 `archive.ArchiveError`），
  以 `OSError` 表达文件操作错误。

`astro-archive --archive PATH` 提供三个互斥操作：

- `--initialize DEFINITION_JSON`
- `--apply BATCH_JSON`
- `--revision N --output REPORT_JSON`

初始化与提交成功打印 JSON 回执；渲染以既有 create-only 语义发布单个 JSON 报告后打印报告
`summary`，退出 0。输入、冲突、损坏档案或文件操作失败退出 2；已有报告绝不覆盖，失败不改写
档案。新 JSON 文件沿用严格读取约定：8 MB 上限、重复键与非有限常量拒绝、未知/缺失字段拒绝、
整数拒绝 bool。

## 档案定义

`definition` 恰含 `dataset_id`、`width`、`height`、`apertures`、`products`：

- width/height（1..64）与 apertures 沿用现有定义；`products` 为 `null` 时是经典档案，
  为数组时完整沿用 campaign products 定义与范围，并引用同一定义中的孔径。
- 定义初始化后不可修改。修订 0 是没有帧和校准记录的合法空档案，也可以渲染。
- ID 沿用非空、不超过 200 字符、精确匹配、不去空格不折叠大小写的字符串规则；dataset、
  dark、flat、release、frame、aperture、product、batch 各属独立命名空间。

## 批次契约

`batch` 恰含 `batch_id`、`expected_revision`、`add_darks`、`add_flats`、
`add_releases`、`retract_releases`、`put_frames`、`withdraw_frames`。
`expected_revision` 为非负整数；其余六项是数组，单个数组最多 50 项，同类 id 在数组内
不得重复，整批至少包含一项变更。

- **add_darks**：每项恰含 `id`、`dark_exposure_s`（1..86400）、`dark`、
  `dark_variance`，两个矩阵与档案同形、元素为 0..10^9 整数。暗场随机量身份是
  “暗场 id、探测器 x、探测器 y”；不同 id 即使矩阵数值完全相同也相互独立。登记后不可
  修改、删除或重用 id。
- **add_flats**：每项恰含 `id`、`flat`、`bad_mask`，同形且沿用现有范围；至少有一个未屏蔽
  且 flat>0 的像素。每个平场的归一因子 M 只由该平场自身的未屏蔽正值确定。登记后同样
  不可修改、删除或重用 id。
- **add_releases**：每项恰含 `id`、`version`（1..10^12，全档案唯一且不可重用）、
  `start_s`、`end_s`（0..10^12，start<end）、`dark_id`、`flat_id`。release 为观测标签
  落在 `[start_s, end_s)` 的帧提供一套校准，可引用已登记或同批新增的 master；引用有效性
  以整批最终状态判断，与六个数组的处理顺序无关。release 不可修改；版本号不要求按提交
  顺序增长，允许补录过去的低版本。
- **retract_releases**：release id 列表，只能撤销已存在且未撤销的 release；不允许同批
  新增并撤销同一 release。撤销保留其时间范围、版本及其引用的 master。
- **put_frames**：每项含 campaign 帧全部字段（`id`、`exposure_s`、`dx`、`dy`、
  `saturation`、`pixels`、`observed_at_s`、`variance`）加 `expected_version`。新帧
  expected_version 必须为 0，成功后 frame_version=1；替换已有帧或恢复撤回帧时必须等于
  当前 frame_version，成功后加 1。替换是完整替换，可更正时间标签与像素等全部事实，不做
  字段合并。经典档案也使用带 `observed_at_s` 与 `variance` 的帧。
- **withdraw_frames**：每项恰含 `id`、`expected_version`，只能撤回当前有效帧且版本匹配；
  成功后版本加 1 并保留撤回状态与旧版本。同一 id 不得在同批 put/withdraw 中同时出现。
- 档案历来出现过的帧 id 总数：经典模式最多 50，campaign 模式最多 14；撤回不释放名额。

任一记录非法、引用缺失或版本冲突都使整批失败，不消耗修订、batch_id 或帧版本，不留下
部分校准事实。

### 批次重试身份

同一 `batch_id` 的重试优先于 `expected_revision`：把 JSON 对象键按 Unicode 码点排序、
数组顺序保留、`ensure_ascii=False`、无额外空白生成规范化文本（`expected_revision`
不参与重试身份比较，因为它在重放时本就可能过时；其余字段含整数正常十进制，输入排版不参与
比较）。该 id 已成功提交且规范化内容完全相同时，返回原回执，即使当前修订已前进；内容不同
则拒绝。对从未成功提交的 id，`expected_revision` 必须等于提交时的当前修订。两个进程用
不同 id 同时提交相同 expected_revision 时，文件锁保证最多一个成功。

## 修订内的校准选择

对每张未撤回帧，在观测标签所在区间的全部可见 release 中先选 `version` 最大者，再看其是否
撤销：

- 没有覆盖 release：`no_release`；
- 最高版本 release 已撤销：`retracted_release`，**不回退到较低版本**；
- 否则 `ready`。

被撤回帧状态为 `withdrawn`，不再选择 release。后来补发更高版本可恢复相应时间段；历史修订
不受影响。只有 `ready` 帧参与科学计算。

## 科学计算与误差身份

每帧使用自身绑定的暗场、平场、屏蔽与暗场曝光。探测器坐标上按屏蔽、零平场、饱和、平移出界
的既有优先级判废；用绑定平场自己的归一因子 M，沿用
`(raw/exposure - dark/dark_exposure) * M/flat` 的标定与有效曝光加权叠加。不得先平均各校准
组图像再等权合并；输出每像素分母仍是该输出像素全部有效贡献的曝光总和。

原始帧像素按各自 variance 独立；同一暗场 id 的同一探测器像素在所有使用它的帧、release 与
产品中共享误差。仅 release_id 或 flat_id 不同不能拆开该随机源；暗场 id 不同则不能因为数组
相同而合并。平场、屏蔽、曝光、偏移与判废均为确定量。通过真实绑定关系传播精确有理数误差，
包括同一暗场经不同平场系数或不同平移对多个输出像素的相关性。

- 经典档案叠加所有 ready 帧；campaign 档案只在 ready 帧中按现有契约决定最优分配。
- 候选产品的逐像素曝光与完整孔径积分方差都使用逐帧绑定后的数值；最终子集图像、孔径测量与
  跨产品协方差与同一绑定一致。校准更正会同时改变信号、像素可用性、共享误差与产品可交付性。
- 负信号、无贡献 null、名义圆盘与 Fraction 字符串口径不变；不引入新的优化目标。

## 报告（schema_version=3）

顶层恰含 `schema_version`、`dataset_id`、`revision`、`frame_bindings`、`report`、
`uncertainty`、`summary`。

- `frame_bindings` 按 frame_id 升序，覆盖该修订出现过的全部帧；每项恰含 `frame_id`、
  `frame_version`、`status`、`release_id`、`dark_id`、`flat_id`。ready 与
  retracted_release 填入被选 release 及两个 master id；no_release 与 withdrawn 的三个
  绑定 id 均为 null。ready 但未被使用的帧同样必须报告。
- 经典模式 `report` 采用现有 schema_version=1 报告字段；新增顶层 `uncertainty` 恰含
  `variance_image`（与图像同形，无贡献为 null）、`aperture_order`（全部孔径 id 升序）、
  `aperture_covariance`（同序方阵）。只有完整孔径返回方差/协方差字符串；不完整孔径所在的
  整行整列为 null。
- campaign 模式 `report` 采用现有 schema_version=2 结构（含产品 stack 与 uncertainty、
  measurement_order、covariance 等），顶层 `uncertainty` 为 null，避免第二份口径。
- 没有 ready 帧也是成功结果：经典 frame_audit 为空、图像全 null、曝光与计数全 0、
  summary.frames=0，uncertainty 的像素与矩阵元素均为 null；campaign 返回空交付、全部产品
  未满足，`unused_frame_ids` 只含未分配的 ready 帧，不混入阻断或撤回帧。
- 顶层 `summary` 恰含 `frames_seen`、`ready_frames`、`blocked_frames`、
  `withdrawn_frames`；blocked 只计 no_release 与 retracted_release。所有有序列表按
  Unicode 码点排序，数值全部精确，不以浮点决定阈值、最优解或平局。

## 持久性、并发与故障边界

档案为仅追加布局：`state.json` 保存定义与当前修订指针，`index.json` 保存批次 id/规范化
文本/回执索引，`revisions/` 保存逐批信封，`darks/`、`flats/`、`frames/` 保存不可变的
原始记录文件，`.lock` 为跨进程文件锁。所有写入经同目录临时文件 + 原子替换；提交顺序为
数据文件 → 批次信封 → 状态指针 → 索引。

- 一次成功提交的科学事实、帧版本、修订与 batch_id 回执一同可见；其他进程或重开档案只能
  看到完整旧修订或完整新修订。状态指针已切换而回执未返回时终止，重开会修复索引并使该批次
  的精确重试找到原回执，不会出现半批或双修订。
- 渲染先在锁内捕获固定修订的完整快照，随即释放锁再做科学计算；计算期间其他进程可以提交，
  报告不会混入新提交的帧像素、master 或撤销状态。
- 旧修订在后续替换、撤销与重开后依然可渲染；不覆盖旧原始数组。无法解释的损坏明确报错，
  不跳过中间修订、不丢已确认批次、不新建空档案。未完成的临时产物可清理，但不破坏已提交
  历史。
