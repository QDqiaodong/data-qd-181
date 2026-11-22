本目录为本项目原创合成样例，数据与代码采用MIT许可。不是观测/生产/个人真实数据，不需网络或外部服务。

- input.json：经典 schema_version=1 的 2×3 两帧标定叠加样例。
- campaign.json：schema_version=2 时间分辨 campaign 样例。2×2 画幅、两张互不重叠时间窗的短曝光，交付 snapshot 与 deep 两个产品；两产品共享同一暗场随机源，故跨产品协方差非零（报告 covariance 非对角项为 "2"），即使它们使用的帧完全不重叠。field 孔径名义圆盘部分越界，仅作普通测量、不参与可交付性。
- archive_definition.json 与 archive_batch_1..4.json：schema_version=3 本地校准档案的可复现批次序列。单像素 1×1 档案，先后演示初始绑定、补发高版本 release 更换暗场（信号 10→8、方差 1→4 并触发产品 max_variance=2 的可交付性翻转）、撤销高版本（帧阻断且不回退）、半段时间窗补发恢复。每个批次都可独立重放，旧修订可重复渲染。
- discovery_definition.json、discovery_config.json 与 discovery_batch_1..3.json：时序发现工作区的可复现样例。单像素三帧 a=10、b=12、c=14，暗场方差 0、原始方差 1；批次 2 把 b 更正为 20（b/c 触发、两 discover、参考只剩 a），批次 3 恢复 b=12（两 retract、参考恢复 c）。与 astro-discovery 的 initialize/advance/snapshot/pending/ack 配套使用。

