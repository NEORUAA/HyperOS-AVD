# HyperOS-AVD Installer v1.2.0

Tag：`installer-v1.2.0`，**正式 Release**，可设为 GitHub Latest。手机 r3、Pad 与 OS3 镜像继续各自独立发版。

本版新增小米 18 Pro 官方 **OS4.0.18.0 / r3** 的安装和 **r2 → r3 保数据升级**，支持对应冻结启动代码、全局 HWUI Vulkan 与独立妙享背屏。既有 OS3、手机 r2 与 Pad OS4 的实例管理继续保留。

- **存储扩容修复**：新建时扩展实际 ext4；旧版“虚拟盘 32 GiB、`/data` 约 6 GiB”先完整备份，再通过解密后的来宾系统检查与修复，保留数据与密钥。修复后已验证 `/data` 约 31 GiB 和冷启动。
- 先校验分卷、备份 userdata / QCOW2 / 加密密钥，再检查旧固件与已知项目模块，避免旧补丁覆盖新系统。
- 保留原实例名称、端口、用户设置及项目模块的禁用 / 删除选择；失败可从升级前备份恢复。
- 手机 r3 默认 6 GiB / 4 核 / 32 GiB，Pad 默认 4 GiB / 4 核 / 32 GiB，资源可调整。
- 保留中文 / English 菜单、自动下载、断点续传和 SHA-256 校验，安装器与镜像分别检查更新。

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

或解压 `HyperOS-AVD-Installer-v1.2.0-macos-arm64.zip`，双击 `Install.command`。需要 Apple Silicon Mac、Python 3 与 Android Studio SDK Emulator / Platform-Tools。新建或扩容用户分区还需 `brew install e2fsprogs`，依赖会在改动前检查。

已加密磁盘暂不支持继续增大虚拟容量（如 32 → 64 GiB）；修复原有 32 GiB 容量不受此限制。新建实例可直接选择更大容量。

升级前须关闭目标 AVD。r3 自动迁移仅开放 r2 来源；旧 revision 1 实验相机桥接需先在 r2 升至 revision 2。没有发布清单的早期源码实例不支持自动迁移；不支持手机 / Pad / OS3 互迁或保数据降级。

附件为安装器 ZIP、`install.sh`、`installer.json` 与 `SHA256SUMS`，不含系统镜像或个人用户数据。

[安装 / 升级 / 恢复指南](https://github.com/NEORUAA/HyperOS-AVD/blob/installer-v1.2.0/docs/installing.md)
