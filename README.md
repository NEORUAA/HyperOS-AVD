# HyperOS-AVD

在 Apple Silicon Mac 的 Android Studio ARM64 模拟器中运行小米 HyperOS，支持 **ADB、KernelSU root、GPU 加速和全局 SELinux Enforcing**。

**手机与平板 OS4 均由小米官方 OTA 原包移植**，保留原厂系统应用、桌面、首次开机引导及 Flutter 玻璃界面；OS3 使用 MysticGSI，适合体验基础功能。

## 版本选择

| | HyperOS 3 | HyperOS 4 手机 | HyperOS 4 Pad |
| --- | --- | --- | --- |
| 系统 | 3.0.2.0.WMCCNXM / Android 16 | 4.0.18.0.XFRCNXM / Android 17 | 4.0.15.0.XBMCNXM / Android 17 |
| 来源 | 小米 13（fuxi）MysticGSI | 小米 18 Pro（hongkong）官方 OTA | Xiaomi Pad 9 Pro Max（yingtian）官方 OTA |
| 显示 | 手机布局 | 主屏 1120×2436 / 480 dpi，背屏 912×596 / 450 dpi | 横屏 3408×2272 / 400 dpi |
| 默认资源 | 2 核 / 2.5 GiB | 4 核 / 6 GiB（负一屏完整模糊建议 8 GiB） | 4 核 / 4 GiB |
| 发布说明 | [兼容范围](docs/compatibility.md) | [v0.2.3 / r4](docs/releases/os4-r4.md) | [v0.1.0 / r1](docs/releases/os4-pad-r1.md) |

Pad 使用独立的 `pad-v*` 版本线；手机与 OS3 的版本号继续沿用 `v*`。各版本独立保存镜像与用户数据，可同时安装。镜像在 [GitHub Releases](https://github.com/NEORUAA/HyperOS-AVD/releases) 分卷提供，勿混用不同 Release 的文件。

## Pad OS4 截图

<table>
  <tr>
    <td align="center" width="33.33%"><img src="screenshots/os4_pad/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-06%20%E4%B8%8A%E5%8D%883.13.44.png" width="360" alt="Pad OS4 横屏桌面与小组件" /><br />平板桌面与小组件</td>
    <td align="center" width="33.33%"><img src="screenshots/os4_pad/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-06%20%E4%B8%8A%E5%8D%883.13.53.png" width="360" alt="Pad OS4 设置中的系统版本与机型" /><br />系统版本与机型</td>
    <td align="center" width="33.33%"><img src="screenshots/os4_pad/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-06%20%E4%B8%8A%E5%8D%883.14.02.png" width="360" alt="Pad OS4 KernelSU 与 SELinux 强制执行" /><br />Root / 强制执行</td>
  </tr>
</table>

## 手机 OS4 截图

**r3：OS4.0.18.0 与独立背屏窗口。**

<img src="screenshots/os4/r3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-06%20%E4%B8%8B%E5%8D%889.15.47.png" width="640" alt="OS4 r3 系统版本与独立妙享背屏窗口" />

以下 4×2 为 r2 的界面参考，r3 / r4 保留这些功能。

<table>
  <tr>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.21.27.png" width="220" alt="OS4 首次开机引导" /><br />首次开机引导</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.21.44.png" width="220" alt="OS4 语言设置" /><br />语言设置</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.23.03.png" width="220" alt="OS4 小米账号" /><br />小米账号</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.25.00.png" width="220" alt="OS4 设置完成" /><br />设置完成</td>
  </tr>
  <tr>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.26.08.png" width="220" alt="OS4 原生桌面与玻璃" /><br />原生桌面与玻璃</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.27.25.png" width="220" alt="OS4 KernelSU / Enforcing" /><br />Root / 强制执行</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.27.44.png" width="220" alt="OS4 系统版本" /><br />系统版本</td>
    <td align="center" width="25%"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.28.03.png" width="220" alt="OS4 全天候息屏" /><br />全天候息屏</td>
  </tr>
</table>

<details>
<summary>OS3 截图</summary>

<table>
  <tr>
    <td align="center" width="25%"><img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.18.png" width="200" alt="OS3 桌面" /><br />桌面</td>
    <td align="center" width="25%"><img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.39.png" width="200" alt="OS3 KernelSU" /><br />Root</td>
    <td align="center" width="25%"><img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.47.22.png" width="200" alt="OS3 设置" /><br />设置</td>
    <td align="center" width="25%"><img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.49.35.png" width="200" alt="OS3 系统版本" /><br />系统版本</td>
  </tr>
</table>

</details>

## 安装与升级

需要 **Apple Silicon Mac、Python 3、Android Studio SDK 的 Emulator / Platform-Tools**。建议预留 60 GiB 加用户数据备份空间，无需 NDK 或手机 OTA；新建 / 扩容用户分区需 `brew install e2fsprogs`。

在终端运行，选择中文 / English 和安装目录即可自动拉取正式安装器：

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

ASCII 菜单自动下载并校验 OS3 / 手机 OS4 / Pad OS4 镜像，可自定义 **AVD 名称、RAM、存储和 CPU**，支持备份后升级与恢复。默认存储 32 GiB；手机 r4 需 **Installer 1.2.1 或以上**，Pad 版需 1.1.0 或以上。也可下载 [Installer ZIP](https://github.com/NEORUAA/HyperOS-AVD/releases) 后双击 **`Install.command`**。

安装器与镜像独立发版：安装器使用 `installer-v*` 正式 Release，镜像继续使用各自的 Pre-release。也可直接从源码运行：

```sh
git clone https://github.com/NEORUAA/HyperOS-AVD.git
cd HyperOS-AVD
chmod +x Install.command
./Install.command
```

**r3 → r4：关闭目标 AVD → Installer 1.2.1 选“升级” → 选择原实例与 r4。** 自动备份 userdata、QCOW2 和加密密钥，保留应用及个人设置，失败可恢复。r1 / r2 / r3 均可直接升级至 r4；已安装旧版实验相机模块时，先按安装指南处理兼容检查。Pad 与手机版不能互相覆盖升级；安装后从安装器或实例目录的 `Start.command` 启动。

[安装 / 升级 / 恢复指南](docs/installing.md) · [Installer 1.2.1](docs/releases/installer-v1.2.1.md) · [发布流程](docs/releasing.md)

## 兼容范围

**手机 OS4：** r4 固化安全中心、定位服务与 SIM 注册的启动崩溃修复，减少缺失硬件接口导致的后台反复重启；保留 r3 全局 HWUI Vulkan、原版妙享背屏、独立窗口、双击息屏 / 唤醒、右侧边缘返回与锁屏编辑预览修复。保留桌面手势、Flutter 玻璃、天气 / 相册、完整引导、60 Hz、Mac 色彩和声音适配；新用户默认常亮与 AOD。

**Pad OS4：** 保留平板布局与原机配置，已核验首次引导、横屏、天气背景与权限弹窗、KernelSU 和冷启动；沿用原厂 `yingtian.xml` 配置，不开启 AOD。

均为实验镜像：Pad 曾出现模拟器 / Vulkan 同步崩溃，长期稳定性仍需观察；相机基础预览与拍照已有适配，手机后摄录像仍掉帧，Pad 拍照保存存在延迟、录像未验收。蓝牙、小米云服务、Google 登录与 Play Integrity 未完整验证，KSU root 域仍为 permissive。应用商店更新未知原生库版本后可能需要重新适配。

[OS3 说明](docs/compatibility.md) · [手机 OS4 适配](docs/hyperos4.md) · [Pad OS4 适配](docs/hyperos4-pad.md) · [Pad 验证记录](docs/releases/os4-pad-r1-validation.md)

致谢：小米、MysticGSI、[Android Emulator / AOSP](https://developer.android.com/studio/run/emulator-commandline)、[KernelSU](https://github.com/tiann/KernelSU)。镜像保留 SDK 的 `NOTICE.txt`，第三方组件归各自权利人所有。
