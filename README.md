# HyperOS-AVD

在 Apple Silicon Mac 的 Android Studio ARM64 模拟器中运行小米 HyperOS，支持 ADB、KernelSU root 和 GPU 加速。

当前镜像：**HyperOS 3.0.2.0.WMCCNXM / Android 16（API 36）**，基于小米 13（`fuxi`）MysticGSI 移植版。项目仍处于实验阶段，镜像通过 GitHub Releases 单独分发。

## 截图

| 桌面 | KernelSU | 设置 | 系统版本 |
| --- | --- | --- | --- |
| <img src="screenshots/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.18.png" width="200" alt="桌面" /> | <img src="screenshots/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.39.png" width="200" alt="KernelSU" /> | <img src="screenshots/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.47.22.png" width="200" alt="设置" /> | <img src="screenshots/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.49.35.png" width="200" alt="系统版本" /> |

## 快速开始

需要 Apple Silicon Mac、Python 3，以及 Android Studio SDK 中的 **Android Emulator** 和 **Platform-Tools**。首次安装预留约 30 GiB 空间，更新约 45 GiB。

1. 下载或克隆本仓库。
2. 从同一个 Release 下载 `manifest.json` 和全部 `.tar.gz.partNNN` 分卷，放在同一目录，无需手动解压。
3. 在仓库目录执行：

   ```sh
   chmod +x Setup.command Start-HyperOS.command
   ./Setup.command --bundle /path/to/release/manifest.json
   ./Start-HyperOS.command
   ```

首次启动会创建空白数据、初始化 KernelSU 并安装管理器；等待终端显示 `HyperOS is ready`。以后可双击 `Start-HyperOS.command`，已有应用和数据会保留。

默认 AVD 为 `HyperOS_3_API_36`，ADB 序列号为 `emulator-5566`。启动器会拒绝占用的端口或属于其它工作区的 AVD，不会停止其它模拟器。SDK 自动检测失败时，安装命令可附加 `--sdk /path/to/Android/sdk`。

## 功能状态

| 功能 | 状态 |
| --- | --- |
| ARM64 原生启动、ADB、GPU | 已验证启动；启用宿主 GPU |
| KernelSU / SELinux | root 可用，全局 Enforcing；KSU root 域仍为 permissive |
| 屏幕 | 1080 × 2400、440 dpi，圆角与挖孔比例已修复 |
| Wi-Fi / 虚拟移动网络 | 基础连接已验证 |
| GPS | 坐标注入、四次启停回归通过；保留小米定位服务 |
| Google Play 服务 | 已包含，登录、认证和 Play Integrity 未验证 |
| 相机 / 音频 / 蓝牙 | 前两项默认关闭；蓝牙未验证 |

网络融合定位、小米云服务及 Android Studio 内嵌窗口未验证。空白数据首次设置曾出现一次锁屏杂志引发的 SystemUI 崩溃，随后自行恢复。详细范围见[兼容说明](docs/compatibility.md)。

## 常用命令

```sh
adb -s emulator-5566 shell "su -c 'id; getenforce'"
adb -s emulator-5566 emu geo fix 114.1733 22.3200
```

`geo fix` 参数顺序为经度、纬度。也可使用模拟器 Extended Controls → Location 注入坐标。

## 其它资料

[重建镜像](docs/rebuilding.md) · [Release 命名与发布模板](docs/releasing.md)

致谢：MysticGSI、[Android Emulator / AOSP](https://developer.android.com/studio/run/emulator-commandline)、[KernelSU](https://github.com/tiann/KernelSU)、[OEM 镜像移植参考](https://github.com/zhuowei/meta-rayban-firmware-android-emulator)。镜像保留 SDK 的 `NOTICE.txt`，第三方组件归各自权利人所有。
