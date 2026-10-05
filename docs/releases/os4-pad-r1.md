# HyperOS 4 Pad · v0.1.0 · Apple Silicon · r1

Tag：`pad-v0.1.0-a17-hyperos4-yingtian-r1`，**Pre-release**。Pad 使用独立的 `pad-v*` 版本线，手机版本号继续沿用 `v*`。系统基包为 Xiaomi Pad 9 Pro Max（yingtian）官方 **OS4.0.15.0.XBMCNXM / Android 17**。

将原厂系统分区与小米扩展移植到 Android Studio ARM64 Emulator，保留平板桌面、系统应用和完整首次开机引导。设备身份、功能 XML 与显示参数来自原包；内核和硬件后端采用 AVD ranchu。

## 本版内容

- 原厂横屏 `3408×2272 / 400 dpi`；默认 **4 GiB RAM / 4 核 / 32 GiB 存储**，可在安装器调整资源。
- KernelSU root、全局 SELinux Enforcing、GPU 加速；ADB 保留授权确认。
- HWUI Vulkan 与 Flutter 文字、玻璃和阴影适配；天气使用独立 ANGLE 桥，原签名 APK 保持不变。
- 60 Hz 合成、macOS sRGB 显示、平板旋转与后台缩略图、小爱边缘光效适配。
- 默认常亮；按原机配置不启用 AOD。序列号按小米样式随机生成，随当前用户分区保存，恢复出厂后重新生成。
- 相机 ANGLE 与前后摄桥接自动初始化。所需原生库、校验收据及冻结启动代码随包提供，无需 NDK 或原始 OTA。

## 安装

需要 **Apple Silicon Mac、Python 3、Android Studio SDK Emulator / Platform-Tools**，建议预留 60 GiB 加备份空间。使用 **Installer 1.1.0 或以上**，在菜单选择 Pad OS4：

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

也可从 Installer ZIP 打开 `Install.command`。等待初始化完成，按提示在虚拟机确认 ADB 授权，再完成首次开机引导。之后从安装器或实例目录的 `Start.command` 启动。

Pad 首版应新建独立实例，不能覆盖升级手机 OS4 或 OS3。镜像附件为所有分卷、`manifest.json` 和 `SHA256SUMS`；文件名保持不变，勿混用不同版本。安装器另行下载，包内不含个人用户数据。

## 验收与限制

本地测试镜像已实际完成原版 OOBE、新用户天气背景及权限弹窗检查，并确认冷启动后城市、权限、机型与序列号保留。发布候选与分卷的验证进度见 [验证记录](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-pad-r1-validation.md)。

Pad 曾出现 MoltenVK 与 native sync 崩溃，当前已采取规避配置，长期稳定性仍需观察。前后摄预览与基础拍照已测试，但原厂拍照后处理可能延迟保存，录像尚未验收。蓝牙、小米云服务、Google 登录及 Play Integrity 未完整验证；KSU root 域仍为 permissive。应用商店更新未知原生库版本后可能需要重新适配。

[安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [Pad 适配与重建](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/hyperos4-pad.md)
