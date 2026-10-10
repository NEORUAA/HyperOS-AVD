# HyperOS Pad 4.0.15.0 · v0.1.1 · Apple Silicon · r2

Tag：`pad-v0.1.1-a17-hyperos4-yingtian-r2`，目标为 **Pre-release**。最终附件已在本地准备，尚未发布；最终冻结 Installer 新装与冷启动已验收，公开 r1 → r2 的实际来宾升级本轮未测。继续使用 Xiaomi Pad 9 Pro Max（yingtian）官方 **OS4.0.15.0.XBMCNXM / Android 17**，保持独立 `pad-v*` 版本线。

## 相比 r1

- 与手机 OS4 共用 **Core 4 / Apps 3** 两个通用 KernelSU 模块；按实际内容、ABI、来源和硬件能力选择规则，保留原签名 APK。已审计天气重装后可自行跟随 PackageManager 路径恢复；未知可选内容局部跳过，不中断其余初始化。
- 新增离线只读状态 WebUI；统一旧模块认证迁移、事务恢复和生命周期处理，保留用户开关与功能选择。核验状态与实际进程加载分别检查。
- ART 编译使用真实 CPU 拓扑；启动策略探测 ranchu 能力，保留真实接口失败与 SELinux 拒绝。
- 保留原厂横屏 **3408×2272 / 400 dpi**、`yingtian.xml`、稳定机型 / 序列号、HWUI Vulkan、Flutter 玻璃、天气、相机预览、60 Hz、macOS 色彩和声音适配。原机不支持的 AOD 不启用。

默认 **4 核 / 4 GiB RAM / 32 GiB 存储**，均可调整，RAM 没有 4 GiB 上限。Phone 的 Vulkan 命令传输参数变更没有用于 Pad；Pad 对照试验未证明性能收益，保持原配置。

## 安装与升级

配套 **Installer 1.2.2**。需要 Apple Silicon Mac、Python 3、Android Emulator / Platform-Tools；新建或扩容用户分区需要 e2fsprogs。无需 NDK、原始 OTA 或生产者缓存。

关闭目标 Pad AVD，备份后通过同系列兼容检查升级至 r2；完整用户分区、QCOW2、加密密钥及配置均保留。不能覆盖手机 OS4 / OS3 或保数据降级。附件为同目录全部镜像分卷、`manifest.json` 与 `SHA256SUMS`；先发布安装器，再发布镜像。

## 验证边界

现有 Pad 已复测通用模块冷启动、天气同 APK 保数据重装、虚拟场景相机预览、状态 WebUI，以及 LicenseActivity 正文显示与滚动。OEM / Parrot 完整拍照、HDR 和录像算法未验收；Parrot 为可选功能，未安装不视为故障。

反馈中的桌面 **6325** 尚无已审计内容规则，FindDevice **20.15.70** 缺少 APK。部分历史 OOBE、MoltenVK / 同步、Gallery 小部件及 FindDevice Job 问题尚未完成对应场景或根因闭环，不因最终抽样未复现就宣称已修复。本轮 headless 测试另出现来宾关机后宿主未自行退出，尚未闭环。

最终附件已完成 headless 新装、冷启动、32 GiB ext4 与模块状态检查；补丁核验后重开应用，实际加载的桌面 / 天气库哈希与 inode 匹配。逐页引导和 Mac 横屏窗口交互未由此测试覆盖。

未下载本地缺失的公开 r1 附件，因此 r1 → r2 仅有通用兼容、备份与生命周期测试，不能视为实际来宾升级通过。

蓝牙、小米云服务、Google 登录及 Play Integrity 未完整验证，KSU root 域仍为 permissive。逐页 OOBE、完整相机算法与长期宿主稳定性需单独验收。

[安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [r2 验证记录](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-pad-r2-validation.md) · [补丁与运行证据](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/os4-patches.md)
