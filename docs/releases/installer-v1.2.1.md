# HyperOS-AVD Installer v1.2.1

Tag：`installer-v1.2.1`，**正式 Release**，可设为 Latest。镜像仍各自使用 Pre-release。

新增手机 OS4 **v0.2.3 / r4** 的安装和 **r1 / r2 / r3 → r4 保数据升级**，按同家族向前升级策略校验修复收据、固件身份与加密模板，后续修订无需逐版列出来源；继续支持 OS3、Pad、旧手机版本和 r2 → r3 升级。

升级前关闭目标 AVD。安装器先保存 userdata、QCOW2、加密密钥和实例配置，失败可恢复；资源调整与旧用户分区容量校验继续保留。不能跨手机 / Pad / OS3 升级或保数据降级。

上传同目录安装器 ZIP、`install.sh`、`installer.json`、`SHA256SUMS`，共 4 件。用户可继续使用公开安装入口，或解压 ZIP 后运行 `Install.command`。

[安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [r4 镜像](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-r4.md)
