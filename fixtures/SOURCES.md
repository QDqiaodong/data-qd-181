本目录为本项目原创合成样例，数据与代码采用MIT许可。不是观测/生产/个人真实数据，不需网络或外部服务。

- input.json：经典 schema_version=1 的 2×3 两帧标定叠加样例。
- campaign.json：schema_version=2 时间分辨 campaign 样例。2×2 画幅、两张互不重叠时间窗的短曝光，交付 snapshot 与 deep 两个产品；两产品共享同一暗场随机源，故跨产品协方差非零（报告 covariance 非对角项为 "2"），即使它们使用的帧完全不重叠。field 孔径名义圆盘部分越界，仅作普通测量、不参与可交付性。

