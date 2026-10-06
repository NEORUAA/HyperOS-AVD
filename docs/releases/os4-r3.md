# HyperOS 4 · v0.2.2 · Apple Silicon · r3

Tag：`v0.2.2-a17-hyperos4-hongkong-r3`，**Pre-release**。基包升级为小米 18 Pro（hongkong）官方 **OS4.0.18.0.XFRCNXM / Android 17**，由原厂系统分区移植，保留系统应用与完整首次开机引导。

## 相比 r2

- **妙享背屏**：新增 `912×596 / 450 dpi` 原版背屏与独立窗口，补齐安全区、右边缘返回及双击息屏 / 唤醒；单击不会误唤醒。
- **渲染**：View 应用全局使用 HWUI Vulkan，保留 Flutter 文字、玻璃、渐进式模糊与阴影修复；修复锁屏编辑预览的绿屏 / 花屏。
- **稳定性**：适配主副屏 60 Hz 合成并修复 goldfish 同步队列溢出，保留桌面手势、近期任务、Mac sRGB 色彩、音频、GPS、传感器和实验相机适配。
- **资源与默认值**：主屏 `1120×2436 / 480 dpi`，默认 **6 GiB RAM / 4 核 / 32 GiB 存储**。新用户默认常亮与 AOD；升级保留用户选择、模拟序列号和应用数据。

## 安装与保数据升级

需要 Apple Silicon Mac、Python 3、SDK Emulator / Platform-Tools；新建 / 扩容另需 `brew install e2fsprogs`，建议预留 60 GiB 加备份空间。使用 **Installer 1.2.0 或以上**，无需 NDK 或原始 OTA：

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

已有 r2：**关闭目标 AVD → 升级 → 选择原实例与 r3**。安装器先备份完整 userdata / QCOW2 / 加密密钥，再启动旧 r2 检查并隔离旧项目补丁，随后切换固件。若使用 revision 1 的实验相机桥接，需先在 r2 更新至 revision 2；不满足兼容检查时会中止迁移。r1 先升 r2，Pad 与 OS3 不能覆盖升级至手机版。

安装完成后通过原实例的 `Start.command` 启动。镜像附件为全部分卷、`manifest.json` 与 `SHA256SUMS`，勿混用版本；安装器另行下载，包内不含个人用户数据。

## 验证与限制

独立 r2 → r3 测试实例已核验应用清单、Android ID、引导状态、模拟序列号、用户设置及测试文件保留；天气数据探针一致。r3 冷启动已确认 root / Enforcing、Settings 的 HWUI Vulkan、背屏独立窗口、原版边缘返回及双击息屏 / 唤醒。升级验证不等于所有账号登录态或云服务验收。

相机仍属实验功能，手机后摄录像掉帧；蓝牙、小米云服务、Google 登录和 Play Integrity 未完整验证。全局 SELinux 为 Enforcing，KSU root 域仍为 permissive。未知应用商店原生库更新、第三方模块或设备硬件专属能力可能需要额外适配，长期稳定性继续观察。

[安装 / 升级 / 恢复指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [发布验证记录](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-r3-validation.md)
