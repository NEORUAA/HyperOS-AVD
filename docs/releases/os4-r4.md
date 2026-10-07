# HyperOS 4.0.18.0 · v0.2.3 · Apple Silicon · r4

Tag：`v0.2.3-a17-hyperos4-hongkong-r4`，**Pre-release**。继续使用小米 18 Pro（hongkong）官方 **OS4.0.18.0.XFRCNXM / Android 17**；本版更新 AVD 适配，不更换手机 OTA 基包。

## 相比 r3

- 修复安全中心在解锁后初始化重复 provider 时的崩溃。
- 修复定位服务请求不存在的 Qualcomm GNSS 扩展时的崩溃与等待，保留模拟器标准 GNSS。
- 修复没有实体 SIM 信息时，小米注册服务的空值崩溃。
- 根据实际内核能力停用无法工作的 OEM 预读、Millet 与 Qualcomm 私有定位守护进程，减少反复启动和无效等待；不关闭受支持的接口。
- 修复写入系统镜像，新装和恢复出厂后仍保留，无需额外安装修复模块。沿用 r3 的全局 HWUI Vulkan、Flutter 渲染、锁屏编辑和独立妙享背屏适配。

## 安装与升级

需要 **Apple Silicon Mac、Python 3、Android Emulator / Platform-Tools、e2fsprogs**，无需 NDK 或手机 OTA。默认 **6 GiB RAM / 4 核 / 32 GiB**，资源可调整。

使用 **Installer 1.2.1 或以上**。已有 r3：关闭目标 AVD →“升级”→ 原实例 → r4。安装器先备份完整 userdata、QCOW2 与加密密钥，再保数据切换；失败可恢复。r1 / r2 也可直接升级至 r4。手机、Pad、OS3 不能互相覆盖，保数据降级不支持。

上传同目录全部镜像分卷、`manifest.json` 与 `SHA256SUMS`，保留文件名；先发布 Installer 1.2.1，再发布本镜像。

## 已知限制

Google 认证 / 登录问题本版未处理，不承诺 Play Integrity。相机和部分云服务仍属实验功能，保留 r3 已知限制。系统 APK 修复保留原签名身份用于系统分区扫描，修改后的 APK 不是可独立侧载的签名更新包；普通用户 APK 的签名验证未改动。

[安装与升级](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [r4 验证记录](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-r4-validation.md)
