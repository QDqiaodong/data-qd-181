# 天文教学图像 v1 与时间分辨 campaign v2

## 模式一：经典叠加（schema_version=1）

输入自定义JSON二维探测器图像，不读取FITS，不声明真实天文测光标定。width/height为1..64，所有matrix必须同形。dark为非负整数暗场，dark_exposure_s为其1..86400秒曝光；flat为非负整数响应，0代表无法校正；bad_mask为布尔矩阵。平场归一因子为未屏蔽且flat>0像素的均值，若无可用像素拒绝。最多50帧，每帧有唯一id、同形非负整数pixels、exposure_s、整数dx/dy（-64..64）及正整数saturation；像素值范围0..10^9，>=saturation不贡献。输入字段严格检查，不接收隐式布尔整数。

逐源探测器像素先按mask、flat零、饱和、平移出界次序排除并分类记数；有效位置输出坐标(x+dx,y+dy)，图像原点左上，x列/y行。像素校正为(raw/exposure-dark/dark_exposure)*mean(flat)/flat，以曝光秒加权均值叠加；精确Fraction输出整数或分数字符串。负信号保留，不能擅自钳零。没有贡献的像素为null，同时输出曝光和贡献帧数，null不能当0。

apertures最多100个，圆心必须在画幅内、整数半径0..64；对整像素圆盘（距离²≤半径²）积分叠加图像的rate，报告越界数和无贡献数。complete仅在整个名义圆盘都有值时成立；无值时sum_rate=null，不返回伪零。没有背景扣除、插值、自动配准、PSF、宇宙线剔除或天球坐标变换。

失败退出2，不覆盖已有目标；所有验证及业务计算完成后同目录临时文件原子硬链接新建JSON。新文件可独立消费，不访问网络。样例是原创2×3阵列，教学用途。

## 模式二：时间分辨 campaign（schema_version=2）

顶层增加可选对象 `campaign` 后即进入 campaign 模式，仍通过同一个 service.run(data) 与 `--source/--output` 使用。没有 campaign 时本文件第一节的全部契约、返回结构与数值保持不变，帧也不允许携带 `observed_at_s`、`variance` 字段（按未知字段拒绝）。

### 输入与身份

campaign 模式下 frames 限 1..14 张，每帧在经典字段之外必须增加：

- `observed_at_s`：0..10^12 的整数时间标签。只按标签是否落入产品时间窗判定，不推导曝光起止时刻。
- `variance`：与 pixels 同形的整数矩阵，元素 0..10^9，是每个原始像素样本自身的方差。

`campaign` 必须且只能含 `dark_variance`、`products`：

- `dark_variance`：与 dark 同形、元素 0..10^9 的整数方差矩阵，描述原始暗场计数（不是除以曝光后的速率）的方差。
- `products`：1..4 条产品，每条恰含 `id`、`start_s`、`end_s`、`priority`、`max_frames`、`requirements`。
  - id 沿用非空字符串规则，在 products 内唯一，不做去空格或大小写归并；产品、帧、孔径分属各自命名空间。
  - start_s/end_s 为 0..10^12 整数且 start_s<end_s，时间窗左闭右开 `[start_s, end_s)`。
  - priority 为 1..10^6 整数；max_frames 为 1..4 整数。
  - requirements 有 1..8 条，每条恰含 `aperture_id`、`min_exposure_s`、`max_variance`。
    - aperture_id 必须精确引用输入 apertures，同一产品内不得重复。
    - min_exposure_s 为 1..345600 整数。
    - max_variance 是非负有理数字符串，格式为 `n` 或 `n/d`；n 为 0..10^12、d 为 1..10^12，十进制无前导零（零本身除外），无符号、空白或小数点，允许未约分（如 `4/2`）。

所有新增对象同样严格拒绝缺字段、未知字段、重复 JSON 键、隐式布尔整数与非法引用。语法合法但业务上无可行交付时是成功结果（可能为空分配），不是输入错误。

### 噪声模型与精确传播

每个原始帧像素是期望为 pixels、方差为该帧 variance 的随机量，各帧各像素彼此独立。暗场每个探测器坐标是期望为 dark、方差为 dark_variance 的随机量；不同暗场坐标彼此独立，并与所有帧像素独立。同一探测器坐标的暗场随机量被所有帧、所有产品共享；经 dx/dy 平移后仍以原探测器坐标认同身份。flat、mask、曝光、偏移、判废结果都是确定量，不传播误差；噪声不会让像素重新跨越饱和阈值。

记平场归一因子 M=mean(flat)。一个被接受的探测器像素 (x,y)（平场 f，帧曝光 e，暗场曝光 D）对输出速率的系数为：

- 原始帧随机量：`M/(f·e)`，其方差贡献为 `variance·M²/f²/e²`。
- 暗场随机量：`-M/(f·D)`，与曝光加权无关；在输出像素的曝光加权速率中，同一暗场源 s 的总系数是各贡献帧 `M/(f·D)·e_k/E` 之和（E 为该输出像素的有效曝光总和）。

输出像素方差 = 各原始帧项之和 + 各暗场源 `dark_variance[s]·g_s²` 之和。孔径积分是输出速率像素之和（不再乘曝光），其方差必须计入同一暗场源在不同输出像素间的相关性；两个孔径或两个产品的协方差同时识别共享的原始帧像素（仅同一产品内可能共享）与共享的暗场像素（跨产品也共享）。先前被 mask、零平场、饱和、平移出界排除的样本既不贡献信号也不贡献噪声。全部运算使用 Fraction 精确有理数，阈值比较与平局不经过浮点。

### 可交付性

一个已交付产品分得 1..max_frames 张落在其时间窗内的帧；任何帧全局最多属于一个产品，也可以不被使用，产品图像只由自己的帧生成。像素排除优先级、平场归一因子（始终来自整个输入标定矩阵的未屏蔽正平场像素，不按产品或选帧重新定义）、负信号、null、局部曝光权重与名义圆盘规则均沿用模式一。

产品可交付，当且仅当其每个 requirement：

1. 对应孔径的整个名义圆盘都在画幅内；
2. 圆盘内每个输出像素的有效曝光之和 ≥ min_exposure_s（逐像素检查，不能用帧总曝光、平均曝光或贡献次数替代）；
3. 整个孔径积分的方差 ≤ max_variance（约束完整积分，不能用各输出像素方差简单相加替代）。

任一 requirement 不满足，该帧子集就不能交付该产品。未列为 requirement 的孔径仍按模式一规则测量，可以不完整，不参与可交付性。

### 全局分配口径

在全部帧不重复的合法分配中，依次：最大化已交付产品 priority 总和 → 最大化已交付产品数 → 最小化已使用帧 exposure_s 总和 → 取分配签名字典序最小。签名按全部产品 id 升序排列，每位置放该产品所获帧 id 的升序列表，未交付产品放空列表；按字符串 Unicode 码点与列表字典序比较，空列表小于非空列表。各级目标严格按先后顺序，不加权、不近似、不逐产品贪心。时间窗与孔径要求可以交叠，必须共同决定。实现上对每产品枚举可行帧子集（仅保留包含意义下的极小可行子集，严格超集不可能最优），再以帧位掩码动态规划求精确最优解；空分配合法。

### 输出报告

campaign 模式返回 schema_version=2 的单一报告，顶层恰含：

- `schema_version`：固定 2。
- `products`：按 product_id 升序，仅列已交付产品。每项含 product_id、升序 frame_ids、`stack`、`uncertainty`。
  - stack 完整采用模式一 schema_version=1 报告结构（rate_image、exposure_s、contributor_count、frame_audit、全部输入 apertures 的测量、summary），但只基于该产品获得的帧真实重算，不是在全量堆栈上筛标签。
  - uncertainty 含 `variance_image`（与图像同形，有贡献处为方差字符串，无贡献处为 null）与 `aperture_variances`（只列该产品 requirements 引用的孔径，按 aperture_id 升序，每项含 aperture_id、variance）。
- `measurement_order`：按 product_id 再 aperture_id 升序，列出所有已交付产品的 requirement 测量，每项含这两个 id；无交付产品时为空列表。
- `covariance`：与 measurement_order 一致的方阵，每格为精确协方差字符串，对角线等于对应 aperture_variances，矩阵对称；即使产品间帧完全不重叠，共享暗场仍可能产生非零协方差，不强行置零。无交付产品时为空列表。
- `unfulfilled_product_ids`、`unused_frame_ids`：未交付请求与未用帧的升序 ID 列表。
- `summary`：恰含 requested_products、delivered_products、total_priority、used_exposure_s、used_frames 五个整数，后两项对被使用的不同帧计数。

CLI 成功时打印所返回的 summary 并退出 0；输入错误退出 2。保持完整验证及计算成功后才创建报告、已有输出绝不覆盖的语义，失败不留下半份报告。
