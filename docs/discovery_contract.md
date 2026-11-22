# 时序瞬变发现工作区（schema_version=1）

`astro_stack.discovery` 与 `astro-discovery` 命令在既有 `service.run`、astro-stack、
本地校准档案的提交/render 契约之外新增一条独立业务，不修改任何既有入口的输入、数值与
返回结构。科学输入只来自一个**已存在档案的固定提交修订**：每次推进先在档案锁内捕获
该修订的完整帧版本、master 与 release 状态，再释放锁做科学计算，计算期间档案的新提交
不会混入结果。仅使用 Python 标准库，不联网，不发邮件或调用外部通知服务。

## 公开接口

- `initialize(path, archive_path, config) -> {"dataset_id", "processed_revision": null}`：
  只创建尚不存在的工作区，固定来源档案与 config，不向来源档案提交任何事实；已有路径不能
  覆盖，初始化失败不留下看似成功的目录。
- `advance(path, revision) -> {"dataset_id", "revision", "first_seq", "last_seq"}`：
  对一个**已提交的精确非负修订**重放完整时序分析。允许从任意合法修订开始、允许跳过中间
  修订，但新修订必须大于已处理修订；已处理修订的精确重试返回原回执，不重发事件、不回退
  当前快照；未处理过的较低修订拒绝。
- `snapshot(path) -> report`：只返回最近一次成功推进的完整结果；从未推进时拒绝，不隐式
  触发计算。
- `pending_events(path, limit) -> {"acknowledged_through", "events"}`：`limit` 为 1..100
  整数，返回大于确认游标的最早若干事件，重复读取不消费事件。
- `acknowledge(path, through_seq) -> {"acknowledged_through"}`：`through_seq` 为 0..当前
  最大 seq；只能推进确认游标，较旧值幂等，越过队尾拒绝。

Python 接口业务错误抛 `ValueError`（含 `WorkspaceError`），文件错误抛 `OSError`。
`astro-discovery --workspace PATH` 提供互斥操作
`--initialize CONFIG_JSON --archive ARCHIVE_PATH`、`--advance REVISION`、
`--snapshot --output REPORT_JSON`、`--pending LIMIT`、`--ack THROUGH_SEQ`。配置文件沿用
严格 JSON 读取（重复键、非有限常量、未知/缺失字段、bool 冒充整数拒绝）与 8 MB 上限。
初始化、推进、pending、ack 打印 JSON 返回值；snapshot 以 create-only 语义发布一个 JSON
后打印 summary；成功退出 0，输入/业务/完整性/文件错误退出 2，已有报告绝不覆盖。

## 配置（初始化后不可修改）

恰含 `aperture_ids`、`bootstrap_frames`、`reference_capacity`、`min_exposure_s`、
`min_abs_delta`、`sigma_squared`：

- `aperture_ids`：1..8 个互不相同、精确引用来源档案定义的孔径 id，存储时按 id 升序。
- `bootstrap_frames`：1..8 整数；`reference_capacity`：bootstrap_frames..8 整数。
- `min_exposure_s`：1..86400 整数。
- `min_abs_delta`、`sigma_squared`：严格正有理数字符串，沿用 `n` 或 `n/d` 语法、分子
  分母 0..10^12、无前导零、无符号/空白/小数点的既有规则。

## 选帧与时序处理

按档案原规则先排除 `withdrawn`、`no_release`、`retracted_release` 的帧（进入
`excluded_frames`，不占参考容量），其余 ready 帧按 `(observed_at_s, frame_id)` 升序
处理；相同观测时间允许多帧，按字符串 Unicode 码点打破平局。经典与 campaign 档案均可作为
来源：本业务对**所有 ready 原始帧**做时序分析，来源档案的 products 不参与选帧。

每张 ready 帧通过自身绑定 release 的暗场、平场与屏蔽生成单帧图像，完整沿用判废优先级
（屏蔽、零平场、饱和、平移出界）、负信号、null 与整数平移口径。仅当 config 中**每个**
孔径的整个名义圆盘都在画幅内、有有效贡献，且圆盘内每个输出像素在该帧的有效曝光不少于
`min_exposure_s` 时，该帧才有参考/检测资格；否则状态为 `ineligible`，既不检测也不进入
参考，不能借其他帧补齐当前曝光的质量缺口。

## 参考成员、检测与精确噪声

维护一个容量为 `reference_capacity` 的参考成员序列。参考产品是当前成员的**原始贡献按
局部有效曝光加权**生成的完整图像与精确误差报告，不是成员图像等权平均，也不是孔径和的
简单平均。候选帧的检测使用接纳它之前的参考，当前帧永不进入自己的比较参考；每个
observation 都带检测前的真实参考产品 `reference_before`，报告另交付 `final_reference`。

- 参考成员少于 `bootstrap_frames` 时，合格帧状态为 `bootstrap`，不检测，进入参考。
- 达到 bootstrap 后，对每个配置孔径计算 `delta = 当前帧孔径积分 − 检测前参考孔径积分`
  与该差值的精确 `variance`。方差计入当前曝光与参考之间的**共享暗场相关性**，也计入
  孔径内输出像素的相关性；参考中同一暗场（身份 `dark_id, 探测器x, 探测器y`）的影响先按
  源身份合并再传播，仅 release_id/flat_id 不同不拆开该随机源，暗场 id 不同则不合并；
  平场与判废视为确定量。不同平场系数/偏移时共享误差只部分抵消。
- 单孔径触发当且仅当 `abs(delta) >= min_abs_delta` 且
  `delta^2 >= sigma_squared * variance`，等号成立；delta 正负均可触发。`min_abs_delta`
  严格正，零差值不会触发（即使 variance=0）；非零差值在 variance=0 时仍按规则比较。
- 任一配置孔径触发则整帧状态为 `triggered`，**整张帧不得进入参考**；所有孔径均未触发
  才是 `quiet` 并进入参考。接纳后超容量只移除最早接纳的成员。未触发孔径的测量仍随帧
  报告；非配置孔径在参考 stack 上仍保留其测量与 null 口径。

所有运算与阈值比较均为精确 `Fraction` 有理数，不经过浮点。

## 科学快照（schema_version 固定 1）

报告恰含 `schema_version`、`dataset_id`、`revision`、`config`、`excluded_frames`、
`observations`、`final_reference`、`active_alerts`、`summary`。

- `excluded_frames` 逐项恰含 `frame_id`、`frame_version`、`status`
  （withdrawn/no_release/retracted_release）。
- `observations` 按处理顺序，逐项恰含 `frame`、`status`、`reference_before`、
  `measurements`。`frame` 谱系恰含 `frame_id`、`frame_version`、`observed_at_s`、
  `release_id`、`dark_id`、`flat_id`。ineligible/bootstrap 的 measurements 为空；
  quiet/triggered 的 measurements 按 aperture_id 升序，每项恰含 `aperture_id`、`delta`、
  `variance`、`triggered`，数值为 Fraction 字符串。
- `reference_before` 与 `final_reference` 同结构，恰含 `members`（接纳顺序谱系）、
  `stack`（当前经典堆栈 schema_version=1 的实际字段，测量档案定义的全部孔径）、
  `uncertainty`（`variance_image`、`aperture_order`、`aperture_covariance`，无贡献像素
  为 null，孔径 id 升序，不完整孔径整行整列为 null）。即使没有成员，也返回全 null 图像、
  全 0 曝光/计数、空审计与正确的空图孔径测量，不用空对象替代。
- `active_alerts` 按 `(frame_id, aperture_id)` 排序，只列当前触发孔径；每项恰含 `frame`、
  `aperture_id`、`reference_members`、`delta`、`variance`，谱系与参考成员都是本次决定
  所依据的版本。
- `summary` 恰含 `ready_frames`、`ineligible_frames`、`bootstrap_frames`、
  `quiet_frames`、`triggered_frames`、`active_alerts`、`reference_frames` 七个整数；
  四种状态计数之和等于 ready_frames，reference_frames 为最终成员数。

## 可确认事件队列

逻辑发现身份是 `(frame_id, aperture_id)`，不含来源修订号、帧版本或 delta。每次新推进把
新快照的 active_alerts 与上一成功快照比较：新身份发 `discover`（before=null），消失身份
发 `retract`（after=null），两边都有但完整 payload 不等发 `revise`，相等不发事件；档案
修订号本身不参与比较，无关校准批次若未改变谱系和测量不产生事件。同一推进的全部变化按
`(frame_id, aperture_id)` 升序，分配工作区内从 1 连续递增的 seq。事件恰含 `seq`、
`revision`、`kind`、`frame_id`、`aperture_id`、`before`、`after`；无事件时回执的
first_seq/last_seq 均为 null。

未确认事件不会被后续更正吞掉或合并：discover 后立刻 retract，即使消费者尚未读取，也必须
按序收到两条。确认只影响投递，不改变科学快照或任何推进回执。

## 持久性、并发与故障边界

工作区为仅追加布局：`state.json` 保存固定定义、来源档案路径、已处理修订指针、全局 seq 与
确认游标；`advances/` 保存逐修订不可变信封（回执 + 事件），`snapshots/` 保存逐修订完整
科学快照，`.lock` 为跨进程文件锁。所有写入经同目录临时文件 + 原子替换；新推进的快照与
信封先以最终名落盘，**替换 `state.json` 是唯一提交边界**。

- 重开工作区只可能看到完整旧状态或完整新状态。提交后调用方未收到返回时，精确重试取到
  原回执，不会再次 discover 或跳号；失败推进不消耗修订或事件号。
- 多个进程并发推进同一修订，文件锁保证只产生一次事件序列；确认与推进并发不丢失游标、
  快照或队列项。
- 重开时重放全部信封校验 seq 连续性、回执一致性与快照存在性；无法解释的损坏明确报错，
  不静默清空队列或重建工作区；提交边界之前的临时产物可安全清理。
- 来源档案后续提交不改变工作区中已交付的快照与历史事件 payload。
