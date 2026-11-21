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

## 数据契约和边界

这是自定义JSON教学图像模型，不读取FITS、不做自动配准或真实仪器绝对测光。 字段、单位、顺序及失败约定详见[契约](docs/contract.md)。

样例全部为原创合成数据，见[来源](fixtures/SOURCES.md)。代码和样例MIT许可；业务命令和测试不访问网络、不启动容器。功能边界以实际源码和契约为准。
