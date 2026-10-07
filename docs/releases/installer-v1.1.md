# HyperOS-AVD Installer v1.1.0

Tag：`installer-v1.1.0`，**正式 Release**，可设为 GitHub Latest。镜像继续使用各自的 Pre-release。

本版新增 **Pad OS4 / yingtian** 的下载、校验和独立实例管理，支持对应 format 3 镜像及冻结启动代码。Pad 默认 4 GiB RAM / 4 核 / 32 GiB 存储，资源可自定义。既有 OS3 与手机 OS4 的安装、备份升级和恢复继续保留。

- 中文 / English ASCII 菜单，自选 AVD 名称、目录、资源与端口。
- 自动下载分卷、断点续传与 SHA-256 校验。
- 按镜像系列检查兼容性；Pad、手机 OS4 与 OS3 分别保存用户数据。
- 安装器更新与镜像更新独立，升级前须关闭目标 AVD。

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

或解压 `HyperOS-AVD-Installer-v1.1.0-macos-arm64.zip`，双击 `Install.command`。需要 Apple Silicon Mac、Python 3 和 Android Studio SDK Emulator / Platform-Tools。

附件为 `install.sh`、安装器 ZIP、`installer.json` 和 `SHA256SUMS`，不含系统镜像或个人用户数据。Pad 镜像需本版或更新安装器；最早从源码构建且没有发布清单的实例不支持自动固件迁移。

[安装 / 升级 / 恢复指南](https://github.com/NEORUAA/HyperOS-AVD/blob/installer-v1.1.0/docs/installing.md)
