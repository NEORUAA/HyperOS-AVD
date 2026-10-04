# HyperOS 4 · v0.2.1 · Apple Silicon · r2

Tag：`v0.2.1-a17-hyperos4-hongkong-r2`，**Pre-release**。系统基包仍为小米 18 Pro 官方 **OS4.0.17.0.XFRCNXM / Android 17**。

## 相比 v0.2.0

- **流畅度**：修复整体动画与滚动卡顿，Android 合成帧率恢复为 60 Hz；修复页面返回时压暗不渐隐。
- **显示效果**：改善 Mac 上的过饱和，补全控制中心、主题商店的渐进式模糊，修复小爱唤起动画黑底。
- **相机**：新增可编辑虚拟场景；可选小米相机适配支持预览、前后切换和基础拍照，后摄录像仍有明显掉帧。
- **声音**：默认启用 Mac 声音输出，修复连续播放一段时间后持续无声。
- **默认配置**：RAM 提升至 6 GiB，补全机型身份与随机模拟序列号，启用负一屏小组件入口；减少 17 个指定标签的日志输出。

## 安装 / 升级

使用独立正式 [Installer](https://github.com/NEORUAA/HyperOS-AVD/releases/tag/installer-v1.0.0)，无需手动下载镜像：

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

已有 v0.2.0：**关闭目标 AVD → 升级 → 选择旧实例和 v0.2.1**，自动备份并保留应用与数据。[中英安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/installer-v1.0.0/docs/installing.md)。

需要 Apple Silicon Mac、Python 3、SDK Emulator / Platform-Tools。建议预留 60 GiB 加备份空间。本 Release 附件为 5 个镜像分卷、`manifest.json` 和 `SHA256SUMS`；安装器独立发布。

相机仍为实验功能。蓝牙、云服务、Google 登录与 Play Integrity 未完整验证，查找设备状态查询不可用。

## English

Compared with v0.2.0: fixes 60 Hz rendering and return-transition dimming, improves macOS color matching and progressive blur, fixes XiaoAI's black background and sustained audio loss, adds editable virtual cameras and an optional Xiaomi camera bridge. Defaults to 6 GiB RAM, adds phone identity and a persistent simulated serial, and reduces selected debug logs. The official OS4.0.17.0 / Android 17 base is unchanged.

Use the separate stable Installer above. Close the target AVD before a backed-up upgrade. Rear video still drops frames.
