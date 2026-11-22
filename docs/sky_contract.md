# 空间背景模型与扣背景交付（schema_version=4）

在 `astro_stack.archive`、`astro_stack.discovery` 与既有 `service.run` 之外新增
独立入口 `astro_stack.sky` 与 `astro-sky` 命令。它只读一个**已提交的精确 archive
修订**，对调用者指定的空天样本拟合一个修订内固定的空间天空平面
`b(x, y) = beta0 + beta_x*x + beta_y*y`（坐标原点为输出图像左上角），再用同一模型
生成扣背景图像、孔径测光和实际交付产品。档案格式、事实写入、discovery 消息以及
`astro-stack`、`astro-archive`、`astro-discovery` 的输入、输出与行为完全不变；全部
运算只使用 Python 标准库且为精确有理数。

## 公开接口

- `astro_stack.sky.render(path, revision, specification) -> report`：渲染一个已提交
  修订的 schema_version=4 报告，不发布文件、不写档案。
- 命令行：`astro-sky --archive PATH --revision N --spec SPEC_JSON --output REPORT_JSON`。
  成功打印顶层 `summary` 并退出 0；输入错误、`sky_inconsistent`、修订/路径非法或读写
  失败退出 2，错误信息可识别。发布沿用 create-only 语义：同目录临时文件 + 原子硬链接，
  目标已存在则拒绝，失败不留下半份报告、不修改任何已有目标。

## specification 契约

specification 恰含 `sky_samples`，为 1..64 项数组；每项恰含 `frame_id`、
`detector_x`、`detector_y`。

- 帧 ID 按档案既有规则精确匹配；坐标必须是图像范围内的非布尔整数。同一原始帧的同一
  探测器坐标不得出现两次。未知字段、重复 JSON 键、非对象、越界坐标、布尔坐标、未知帧、
  非法修订或非法路径一律拒绝整份请求。
- 修订内的校准选择沿用既有规则：未撤回帧在覆盖其观测标签的可见 release 中取最高版本，
  再检查撤回屏障；逐帧 dark/flat 绑定随之确定。每个样本所在帧必须是 `ready`，且该探测器
  像素通过既有的屏蔽、零平场、饱和、平移出界判定，否则整份请求失败——绝不替换样本或
  回退标定。
- 样本先按该帧绑定完成标定，空间坐标使用注册后的**输出坐标**。样本先按
  `(frame_id, detector_y, detector_x)` 排序，输入顺序不影响任何结果。
- 空天样本由调用者保证无目标信号；程序不自动挑选样本、不剔除离群点、不做残差阈值检验。
  样本不额外筛选科学帧，不占用产品的 `max_frames` 或互斥名额，也可以来自最终未分配给
  任何产品的帧。整个修订共用这一个固定平面，不跨修订沿用拟合，不自动选择模型或引入
  时变参数。

## 噪声身份与样本协方差

随机量身份与档案契约一致：

- 原始帧像素身份是 `(frame_id, detector_x, detector_y)`，按各自原始计数方差独立；
- 暗场像素身份是 `(dark_id, detector_x, detector_y)`；不同 release 指向同一 dark_id
  仍共享同一暗场随机量，数值相同但 dark_id 不同则相互独立。

平场、判废、曝光与注册偏移是确定量。标定速率
`(raw/exposure - dark/dark_exposure) * M/flat` 被写成基础独立噪声变量上的线性型：
原始计数噪声系数是 `(M/flat)/exposure`，暗场噪声系数是
`-M/(flat*dark_exposure)`（即预计算中的 `-dark_h`，帧曝光不除暗场项）。样本协方差 C
由这些线性型按身份精确合并得到，**不能把输入的原始计数方差直接当成速率方差**。同一样本
同时进入拟合与科学测量时，它始终是同一个随机量，相关项必须保留。

## 支撑、可估计性与 BLUE

允许 X 秩不足、C 奇异，也允许所有噪声为零；所有判定都是精确有理数决定，不使用浮点
伪逆、epsilon、任意置零未识别斜率，也不因秩不足拒绝全部测量。

1. **噪声支撑检查（严格线性，不含阈值）**：必须存在 beta 与 e，使
   `y = X*beta + e` 且 `e` 属于 C 的列空间。实现上求解 KKT 系统
   `[C X; X' 0] [u; b] = [y; 0]`，不一致时以可识别的 `sky_inconsistent` 错误拒绝整份
   分析。C 正定时任意残差都通过该检查。
2. **可估计性**：背景线性目标 `l*beta` 仅当 `l` 属于 X 的行空间时可估计。报告以 X 行
   空间按列顺序 `beta0、beta_x、beta_y` 的非零 RREF 行作为确定 `basis`。
3. **最佳线性无偏估计**：可估计时在全部满足 `a'X = l` 的权重中最小化 `a'C a`，估计值
   为 `a'y`。最小权重不唯一时，在通过支撑检查的数据上估计值与其协方差唯一。

`sky_fit` 是可独立核查的拟合交付，恰含：

- `samples`：每项恰含 `frame_id、detector_x、detector_y、output_x、output_y、dark_id、
  flat_id、rate`（顺序固定），报告原始身份、绑定 master、输出坐标与标定速率；
- `rank`；
- `basis`：有理数字符串二维数组；
- `estimates` 与 `covariance`：按 basis 顺序的估计值（约分 Fraction 字符串）与完整
  估计协方差矩阵。C 奇异时仍给出合法结果。

## 扣背景测光

沿用既有有效像素、逐输出像素的局部曝光加权与名义圆孔径。先以既有权重形成科学测量 q
（单输出像素速率，或完整孔径内这些速率之和），再以完全相同的权重形成该测量对应的平面
背景目标 `l*beta`：

- `l` 可估计时输出 `q - a'y`（同一随机量的完整线性噪声传播）；不可估计时该测量只标
  `unestimable`，不补造任意斜率值。
- 完整孔径作为**一个整体**判断可估计性：单像素不可估计不代表孔径和不可估计，也不能先
  删除这些像素再积分。无贡献或名义圆盘不完整仍是原有覆盖问题，背景模型不补造信号、
  曝光或贡献数。
- 所有方差与协方差都来自最终扣背景测量的完整线性传播，同时计入科学量与拟合量之间、
  不同科学量之间、不同背景预测之间的相关项；允许负协方差与精确零方差。帧分配互斥不
  等于原始帧噪声跨产品独立——同一空天样本可参与多个背景预测。
- 拟合始终使用请求的固定样本集合，不随候选子集、产品是否交付或实际使用的科学帧重新拟合。

## 报告（schema_version=4）

顶层恰含 `schema_version、dataset_id、revision、frame_bindings、report、uncertainty、
summary、sky_fit`；除新增 `sky_fit` 外沿用 schema_version=3 的布局，`frame_bindings`
与帧状态计数语义不变。

### 每个 stack（经典即 report 本身）

- 新增与图像同形的 `pixel_status`，取值 `ok`、`uncovered`、`unestimable`。`uncovered`
  的曝光与计数为零；`unestimable` 保留真实曝光与计数；后两者的 `rate_image`、
  `variance_image` 元素为 null。
- `frame_audit` 与覆盖 `summary` 仍只基于真实接受贡献，`unestimable` 不算无贡献。
- 每个原 `apertures` 项新增 `status`，取值 `ok`、`incomplete`、`unestimable`。
  `nominal_pixels、outside_pixels、missing_pixels、measured_pixels、complete` 继续只
  描述物理覆盖，不从扣背景图像的 null 重算。物理不完整优先 `incomplete`；完整但背景目标
  不可估计为 `unestimable`；其余为 `ok`。仅 `ok` 项的 `sum_rate` 返回完整扣背景积分，
  其余为 null。
- 经典 `uncertainty.aperture_order` 仍覆盖全部孔径；非 ok 项在
  `aperture_covariance` 中的整行整列为 null。

### campaign

- 仍执行既有互斥分配、时间窗、`max_frames` 与各级最优排序，但每条 requirement 的可行
  子集必须同时满足：原覆盖与 `min_exposure_s` 满足、**整个孔径背景目标可估计**、最终
  完整扣背景孔径方差不超过 `max_variance`（等号满足即可行）。不能先用未扣背景方差选
  分配再改显示值；背景估计噪声既可能使原可行产品不可交付，也可能通过相关抵消使原不可行
  产品可交付。无可行产品是成功的空交付。
- `measurement_order、covariance、aperture_variances` 仍只按已交付产品的 requirements
  形成；产品 stack 中的其他孔径同样给出状态与（可能为 null 的）值。选中产品的图像、
  孔径值、方差与协方差对应实际被分配的帧。

## 修订边界与失败约定

- 渲染先在档案锁内捕获固定修订的完整快照再释放锁计算；分析过程不改写档案，检查旧修订时
  不借用后来修订的帧像素、方差或标定。
- 任一输入或样本身份非法、样本未就绪/被判废、噪声支撑不成立，都使整次分析失败，不产生
  报告文件。`SkyError`（含 `SkyInconsistent`）是 `ValueError` 的子类；文件错误以
  `OSError` 表达，命令行统一以退出码 2 返回。
