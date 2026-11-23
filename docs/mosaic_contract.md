# 亚像素巡天拼图（schema_version=1）

在 `astro_stack.archive`、`astro_stack.sky` 等既有入口之外新增独立入口
`astro_stack.mosaic` 与 `astro-mosaic` 命令。它把若干晚各自存放在本地校准档案中的
曝光，按调用者给定的公共坐标原点与逐帧亚像素修正，拼成一张公共平面上的新图，并对
请求矩形给出可用于合并观测的区域测光。档案只读；既有入口、输出与档案格式完全不变；
仅使用 Python 标准库，全部几何、积分与误差运算都是精确有理数。

公共坐标由调用者给定：本模式不做自动配准、不做背景拟合、不处理球面坐标，也**不**把
各晚已叠加好的整数网格图移位或插值——每个通过判废的探测器样本都直接投影到公共平面。

## 公开接口

- `astro_stack.mosaic.render(specification) -> report`：渲染 schema_version=1 拼图
  报告，不发布文件、不写档案。业务/输入/档案错误以 `ValueError`（含
  `mosaic.MosaicError` 与 `archive.ArchiveError`）表达，文件错误以 `OSError` 表达。
- 命令行：`astro-mosaic --spec SPEC_JSON --output REPORT_JSON`。成功打印顶层
  `summary` 并退出 0；输入、档案或文件错误退出 2。发布沿用 create-only 语义：同目录
  临时文件 + 原子硬链接，目标已存在则拒绝，失败不留下半份报告、不修改任何已有目标。
  输入 JSON 沿用既有严格约定：8 MB 上限、拒绝重复键与非有限常量。

## specification 契约

specification 恰含 `sources`、`grid`、`regions`、`min_visits`。未知或缺失字段、
重复 JSON 键、非法引用或不可读来源都使整份请求失败；整数一律拒绝 bool。

### sources（1..8 项）

每项恰含 `archive`、`dataset_id`、`revision`、`origin_x`、`origin_y`、`offsets`：

- `archive`：非空本地路径字符串；相对路径按调用进程工作目录解析。
- `dataset_id`：沿用项目精确匹配口径（非空、不超过 200 字符），在请求内唯一，且必须
  与实际档案一致。
- `revision`：精确的已提交修订，非负整数，上限 10^12；不接受未来修订。
- 仅支持经典档案（`products` 为 null）。**一个来源就是一个 visit**，一个 visit 内可以
  有多张帧。
- `origin_x`、`origin_y`：该档案注册坐标原点在公共平面上的位置（有理数字符串）。
- `offsets`：0..50 项，每项恰含 `frame_id`、`frame_version`、`dx`、`dy`。`dx`、`dy`
  是该曝光相对已存整数注册偏移的额外修正，范围 [-1,1]；未列出的修正为零。`frame_id`
  在同一来源的 offsets 内唯一；`frame_id` 与 `frame_version` 必须匹配该修订实际可见
  版本——被撤回或阻断的帧可以匹配修正但不贡献，未知帧或版本不符都使整份请求失败，
  绝不会把修正默默移给新版本。版本为正整数，上限 10^12。

### 坐标字符串

公共坐标与修正用十进制有理数字符串 `n` 或 `n/d`：允许负分子、允许未约分；分子绝对值
和正分母均不超过 10^12。不接受加号、前导零、负零、小数或空白。

### grid、regions 与 min_visits

- `grid` 恰含 `x0`、`y0`、`width`、`height`；`x0`、`y0` 是左上角的公共坐标，
  `width`、`height` 各为 1..64 的整数。第 (行 i, 列 j) 格是半开单位方格
  `[x0+j,x0+j+1) × [y0+i,y0+i+1)`。
- `regions` 为 0..16 项，每项恰含 `id`、`x0`、`y0`、`x1`、`y1`，在公共平面上指定
  非空半开矩形（`x0<x1`、`y0<y1`）。各 `id` 唯一；矩形可以重叠、可以切开格子、可以
  在 grid 之外。档案内的孔径定义不用于拼图。
- `min_visits` 是 1..len(sources) 的整数。

## 覆盖与叠加

固定每个来源的修订，按既有最高版本校准、撤销屏障与逐帧绑定判断 ready。对每个 ready
帧的探测器样本，继续按屏蔽、零平场、饱和的顺序判废；通过者按完整平场的原归一因子 M
计算原来的精确标定速率 `(raw/exposure - dark/dark_exposure) * M/flat`，可为负。

探测器像素 (x,y) 在公共平面上覆盖一个单位方格，左上角是
`(x + 帧原 dx + origin_x + 该帧额外 dx, y + 帧原 dy + origin_y + 该帧额外 dy)`。
旧单档案整数图的画幅不再裁剪这些像素——原来被单晚画幅裁掉的边缘可以进入拼图。样本
按实际交叠面积贡献；只有边或角接触不产生面积。

对请求范围内几乎处处的每个点，只数在这一点有有效样本覆盖的**不同 visit**。达到
`min_visits` 的面积称为合格面积：

- 一个 visit 的多张曝光即使投到同一处也只算一个 visit；但在合格位置，每张有效曝光都
  按原曝光秒数进入局部加权平均。局部速率的分母是当前点实际覆盖的样本曝光之和。
- 在每个区域内先算这个逐点速率，再对合格面积积分。不能先把曝光乘覆盖面积在整格汇总
  后只除一次，不能把同一 visit 的几张帧当成几次独立覆盖，也不能把只接触边界或覆盖
  不相交的样本当成共同覆盖。

**画格**交付合格部分积分，让下游可看见边缘的真实测量及剩余空缺。**区域测光**只交付
名义矩形的完整积分：区域内只要有正面积不合格，就报告没有完整测量，不用局部归一化或
视作零来补齐。区域覆盖和积分均按它自己的几何边界计算，不从已输出的画格均值或状态
推算。

## 误差与噪声身份

保留现有线性噪声语义。原始计数随机源以 `(dataset_id, frame_id, frame_version,
探测器 x, 探测器 y)` 识别；暗场随机源以 `(dataset_id, dark_id, 探测器 x, 探测器 y)`
识别。同一源无论分到几块、经几条 release 或出现在几个重叠区域，都还是同一个随机量；
两个档案里的同名 dark_id 不共享噪声，同名帧也不能跨档案混同。未共享的源互相独立；
平场、方差参数、质量判废、偏移和曝光为确定量。

每个样本的标定速率是基础独立噪声变量上的线性型：原始计数系数
`(M/flat)/exposure`，暗场系数 `-M/(flat*dark_exposure)`。方差与协方差按实际局部
曝光分母、面积、标定和平场系数精确传播；输出有理量用约分字符串。

## 报告（schema_version=1）

顶层恰含 `schema_version`、`min_visits`、`sources`、`grid`、`cells`、`regions`、
`region_covariance`、`summary`：

- `sources` 只列 `dataset_id` 与 `revision`，按 ID 升序，不含本机路径。
- `grid` 回显规范化参数（`x0`、`y0` 为约分字符串）。
- `cells` 为与 grid 同形的二维行列数组；格内恰含 `qualified_area`、`status`、
  `integral`、`variance`。`status` 为 `uncovered`（合格面积零）、`partial`（大于零
  小于 1）、`complete`（等于 1）；仅 `uncovered` 的积分及方差为 null。
- `regions` 按 `id` 升序，项恰含 `id`、`area`（名义面积）、`qualified_area`、
  `status`、`integral`、`variance`；`status` 按合格面积与名义面积比较用同一组名称，
  只有 `complete` 才给积分和方差。
- `region_covariance` 为与 regions 同序的对称方阵；只有两端均 `complete` 才有值，
  对角等于各自方差。
- 积分是公共平面上的速率面积积分，不除以测得面积，不额外乘曝光。没有合格点仍是成功
  结果。
- `summary` 恰含 `sources`、`ready_frames`、`accepted_samples`、`covered_cells`、
  `complete_cells`、`complete_regions`。

## 稀疏性与修订边界

坐标相隔很远不影响局部交付：请求只需要指定的格网及矩形。实现按每帧投影原点用算术
求出与目标相交的探测器范围，并在样本边诱导的直线划分上扫描，绝不为观测之间的空天
分配致密画幅、枚举空天的单位格或遍历共同包围盒的每一个坐标。有理数区域边缘是原生
对象；整数坐标长度或分母都不是采样分辨率。

渲染先在档案锁内捕获固定修订的完整快照再释放锁计算；分析过程不改写档案。新修订阻断
或替换参与的帧后，不借用旧绑定，也不把匹配旧 `frame_version` 的额外偏移移给新版本；
按旧修订查询仍重现原结果。
