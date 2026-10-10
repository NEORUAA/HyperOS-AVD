# HyperOS-AVD Installer v1.2.2

Tag：`installer-v1.2.2`，目标为 **正式 Release / Latest**。当前仅本地准备，尚未发布；最终附件的两端 headless 新装及手机 r4 → r5 保数据升级已验收；Pad r1 → r2 实际升级本轮未测。镜像继续分别使用 Pre-release。

## 相比 1.2.1

- 修复 [#123](https://github.com/NEORUAA/HyperOS-AVD/issues/123) 中 Python CA 环境引起的 `CERTIFICATE_VERIFY_FAILED`：仅在证书校验失败时回退至 macOS 系统 HTTPS 校验，仍验证证书、域名、附件大小和 SHA-256，尊重显式 CA 配置。
- 支持已有元数据加密用户分区继续扩容，例如 **6 → 32 GiB**。先核验完整离线备份，事务化扩大磁盘容器并保留旧字节与密钥，再在同一受控来宾内扩展解密 ext4；以实际容量、UUID 与文件探针验证结果为准，不只修改虚拟盘大小。
- 配套手机 **v0.2.4 / r5** 与 Pad **v0.1.1 / r2**，携带冻结启动代码与通用模块支持；用户消费包不依赖 NDK 或原始 OTA。继续支持 OS3、旧版镜像和同系列向前升级。

升级或扩容前关闭目标 AVD。安装器保存完整 userdata、QCOW2、加密密钥及实例配置；中断可恢复，发现激活后新增写入时保留现场，不自动用旧数据覆盖。未知磁盘链、备份不完整、模块内容不明或兼容检查失败时拒绝继续。不支持跨手机 / Pad / OS3 升级或保数据降级。

## 附件与状态

上传同目录 **Installer ZIP、`install.sh`、`installer.json`、`SHA256SUMS`**，共 4 件。发布后可使用公共安装入口，或解压 ZIP 后运行 `Install.command`；本地准备状态不代表公开入口已更新。

TLS、磁盘事务、恢复和受控来宾容量路径已有定向回归；最终安装器 ZIP 的 108 项源码、收据、权限、哈希，以及双端新装、手机 r4 → r5 升级与备份恢复通过；逐页 OOBE、Pad r1 → r2 实际升级与长期稳定性未由本轮自动测试覆盖。1.2.1 已能读取 format 3 冻结运行包，但不包含本页新增 TLS 回退与加密磁盘增长路径；新镜像要求 1.2.2 以保证配套流程一致。

[安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [手机 r5](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-r5.md) · [Pad r2](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-pad-r2.md)
