# HyperOS-AVD Installer v1.0.0

建议 Tag：`installer-v1.0.0`，作为**正式 Release**，不要勾选 Pre-release。安装器独立发版；OS3 / OS4 镜像保持各自版本和 Pre-release。

## 功能

- 中文 / English ASCII 菜单，镜像选择同时展示 OS3 与 OS4、历史版本和发布状态。
- 自动下载镜像、断点续传与 SHA-256 校验，无需手动下载分卷。
- 自选 AVD 名称、安装位置、RAM、存储与 CPU；每个实例独立。
- v0.2.0 保数据升级：备份 userdata、QCOW2 和加密密钥，支持失败恢复与回滚。
- 分别检查镜像更新和正式安装器更新；识别旧源码 OS3 / OS4 实例。

下载 `HyperOS-AVD-Installer-v1.0.0-macos-arm64.zip`，解压后双击 `Install.command`。需要 Apple Silicon Mac、Python 3 和 Android Studio SDK 的 Emulator / Platform-Tools，建议预留 60 GiB 加数据备份空间。

已有 Release 安装实例：**关闭目标 AVD → 升级 → 选择实例和新版镜像**。最早从源码构建且没有发布清单的实例可启动或调整硬件，自动固件迁移会被拒绝。安装器更新入口提供下载链接；更换安装器不改已有 AVD 数据。

附件仅有安装器 ZIP、`installer.json` 和 `SHA256SUMS`，不含系统镜像。详情：[中英安装 / 升级 / 恢复指南](https://github.com/NEORUAA/HyperOS-AVD/blob/installer-v1.0.0/docs/installing.md)。

## English

A separate stable installer for Apple Silicon macOS. The bilingual ASCII menu lists both OS3 and OS4 firmware, downloads and verifies archive parts automatically, and supports custom instances/resources, backed-up v0.2.0 upgrades and recovery. Firmware prereleases and stable installer updates are checked separately.

Extract the installer ZIP and open `Install.command`. Python 3 and Android Studio SDK Emulator / Platform-Tools are required. Close the target AVD before upgrading. Legacy source workspaces without a release manifest are listed for start/hardware settings; automatic firmware migration is refused. Firmware is downloaded after selection and is not included in this installer release.
