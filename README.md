# data-qd-181 · 天文图像标定与叠加

从暗场、平场、曝光和整数注册偏移生成叠加图像、贡献审计及孔径积分；可选 campaign 模式从同一批短曝光生成互不重复用帧的时间分辨产品，并给出精确有理数不确定度与跨产品协方差。新增本地校准档案以原子批次登记暗场、平场、release 与帧事实，按任意已提交修订重新生成可追溯的 schema_version=3 报告。

## 独立安装

Python >=3.11，实际验证使用本地CPython3.12.14。运行与unittest仅用标准库，不依赖外部服务。每仓建立独立虚拟环境；构建工具锁定版本，公共包缓存可复用。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

离线安装需要已安装的兼容Python及缓存中的setuptools==80.9.0、wheel==0.45.1。若缓存缺失，应先准备公共构建工具，不能把以上命令当空缓存可用承诺。修改源码后须重新安装本仓。

## 演示

在仓库根目录执行，目标路径须不存在；重复演示使用新文件名。

```sh
.venv/bin/astro-stack --source fixtures/input.json --output run/report.json
```

预期：2帧、6个有贡献像素、9次像素贡献；star孔径为40/3单位每秒。

时间分辨 campaign 使用同一命令与新样例：

```sh
.venv/bin/astro-stack --source fixtures/campaign.json --output run/campaign-report.json
```

预期：请求2个产品、交付2个，snapshot 与 deep 各得一张互不重复的帧；报告 schema_version=2，含每产品 stack、variance_image、aperture_variances、measurement_order 与对称协方差方阵。暗场误差在产品间相关，协方差不会仅因帧不重叠而置零。

成功退出0；业务质量提示或诊断不等于输入失败，见summary。非法输入或已有目标退出2，保留旧目标。JSON最多8MB，拒绝重复键、未知字段及非有限常量。先完整验证与计算，再同目录临时文件原子硬链接新建一个JSON报告。run/已忽略。

## 本地校准档案

`astro-archive` 与 `astro_stack.archive` 提供独立的档案入口，沿用相同的严格 JSON 约定与
create-only 发布语义，不改变上面两种既有模式：

```sh
.venv/bin/astro-archive --archive run/demo-archive \
  --initialize fixtures/archive_definition.json
.venv/bin/astro-archive --archive run/demo-archive \
  --apply fixtures/archive_batch_1.json
.venv/bin/astro-archive --archive run/demo-archive \
  --apply fixtures/archive_batch_2.json
.venv/bin/astro-archive --archive run/demo-archive \
  --revision 1 --output run/archive-rev1.json
.venv/bin/astro-archive --archive run/demo-archive \
  --revision 2 --output run/archive-rev2.json
```

可复现样例是四个连续批次：建立暗场/平场/release 与帧 → 补发高版本 release 更换暗场 →
撤销该高版本 → 补发只覆盖半段时间窗的更高版本。修订 1 信号 10、方差 1 且产品可交付；
修订 2 同一帧信号变 8、方差 4，超过产品 `max_variance=2` 后不再交付；修订 3 帧因最高版本
被撤销而阻断（不回退旧版本）；修订 4 半段窗口内恢复就绪。任何旧修订都可随时重新渲染，
结果与当时交付一致。字段、状态机、重试身份、并发与崩溃恢复边界详见
[档案契约](docs/archive_contract.md)。

## 时序瞬变发现

`astro-discovery` 与 `astro_stack.discovery` 在不改变上面任何入口的前提下，对一个固定
档案修订做逐曝光瞬变发现，并维护不被已发现事件污染的参考图像与可确认本地事件队列。
科学输入只来自已有档案的固定修订，复用档案的逐帧校准选择与原始事实，调用者无需另写
速率、方差或绑定表；经典与 campaign 档案均可作为来源，且对所有 ready 原始帧做时序分析
（来源 products 不参与选帧）。

```sh
.venv/bin/astro-archive --archive run/discovery-archive \
  --initialize fixtures/discovery_definition.json
.venv/bin/astro-archive --archive run/discovery-archive \
  --apply fixtures/discovery_batch_1.json
.venv/bin/astro-discovery --workspace run/discovery-workspace \
  --initialize fixtures/discovery_config.json --archive run/discovery-archive
.venv/bin/astro-discovery --workspace run/discovery-workspace --advance 1
.venv/bin/astro-discovery --workspace run/discovery-workspace \
  --snapshot --output run/discovery-snapshot.json
```

可复现样例是三帧 a=10、b=12、c=14（单像素、flat=1、暗场均值 0、曝光均 1、原始方差 1、
暗场方差 0），配置 bootstrap=容量=1、`min_abs_delta="1"`、`sigma_squared="4"`。修订 1
时 a 为 bootstrap，b/c 均 quiet（差值 2、方差 2），最终参考只有 c；批次 2 把 b 更正为 20
后，b、c 都相对 a 触发（差值 10 与 4、方差 2），整帧不进参考，发出 seq 1、2 两个
discover，最终参考只有 a；批次 3 把 b 恢复为 12 后，即使前两条 discover 尚未确认，也追加
seq 3、4 两个 retract，最终参考恢复 c。`--pending N` 可分段重读而不消费事件，`--ack K`
持久推进确认游标；已处理修订的精确重试返回原回执、不重发事件。参考产品按成员原始贡献的
局部有效曝光加权生成，差值方差精确计入共享暗场（按 `dark_id, 探测器坐标` 认同身份）与
孔径内像素相关性，全部运算为精确有理数。字段、阈值口径、事件身份、并发与崩溃恢复边界
详见[发现契约](docs/discovery_contract.md)。

## 空间背景模型与扣背景交付

`astro_stack.sky` 与 `astro-sky` 在不改变上面任何入口的前提下，对一个固定档案修订用
调用者指定的空天样本拟合修订内唯一的天空平面 `b(x,y)=beta0+beta_x*x+beta_y*y`，再用
同一模型生成扣背景图像、孔径测光与实际交付产品，输出 schema_version=4 报告：

```sh
.venv/bin/astro-archive --archive run/sky-archive \
  --initialize fixtures/sky_definition.json
.venv/bin/astro-archive --archive run/sky-archive \
  --apply fixtures/sky_batch_1.json
.venv/bin/astro-sky --archive run/sky-archive \
  --revision 1 --spec fixtures/sky_spec.json \
  --output run/sky-report.json
```

可复现样例是 3×3 单帧：中心速率 10、四臂 12/8/13/7，五个原始计数方差均为 1、暗场方差 0，
唯一空天样本为帧 a 的中心 (1,1)。半径 1 的 star 孔径背景目标可估计，扣背景积分恰为 0、
方差 20；四个臂像素与半径 0 的 arm 孔径覆盖完整但背景目标不可估计，只给状态不给任意
斜率值。秩、噪声支撑、可估计性与最小方差全部由精确有理数线性代数决定，允许奇异协方差、
零噪声与秩不足；同一样本同时进入拟合和科学测量时保留完整相关传播，campaign 可行性按
最终扣背景方差（含与拟合的相关抵消）重新判断。字段、样本身份、失败边界与报告结构详见
[空间背景契约](docs/sky_contract.md)。

## 离线可复算发布包

`astro_stack.release` 与 `astro-release` 在不改变任何既有入口的前提下，从经典档案的固定
修订交付一块科学图像及离线复算所需的最小证据，仅用标准库：

```sh
.venv/bin/astro-archive --archive run/release-archive \
  --initialize fixtures/release_archive_definition.json
.venv/bin/astro-archive --archive run/release-archive \
  --apply fixtures/release_archive_batch_1.json
.venv/bin/astro-release issue --library run/release-library \
  --archive run/release-archive \
  --request fixtures/release_request.json \
  --license fixtures/release_license.json
.venv/bin/astro-release export --library run/release-library \
  --request-id example-release-0001 --output run/release-package.json
.venv/bin/astro-release verify --package run/release-package.json \
  --expected-sha256 <可信回执中的 sha256>
```

可复现样例是 2×1 两帧（F 不平移、G 右移一格，单位平场、共享暗场 d）：无许可限制时速率
`[10, 25]`、方差 `[5, 5/2]`、区域和 35、区域方差 23/2。把
`fixtures/release_license_restricted.json`（禁止 G 当前版本的探测器 (0,0)）配给新请求号后，
第一格仍发布 10/5，第二格只交付 `withheld` 状态且不带任何样本证据，区域和与方差变为 10、5；
禁止 G 的 (1,0) 不阻断任何格，因为该样本被平移到画幅外。verify 先核可信摘要，再从包内证据
独立重算坐标映射、标定、曝光加权与含共享暗场相关性的误差传播；同号请求永久绑定同一包，许可
替换与源档案移走都不影响已签发回执。包格式、状态语义、证据最小化与故障边界详见
[发布契约](docs/release_contract.md)。

## 数据契约和边界

这是自定义JSON教学图像模型，不读取FITS、不做自动配准或真实仪器绝对测光。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
