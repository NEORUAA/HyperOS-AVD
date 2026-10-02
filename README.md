# HyperOS-AVD

在 Apple Silicon Mac 的 Android Studio ARM64 模拟器中运行小米 HyperOS，支持 **ADB、KernelSU root、GPU 加速和全局 SELinux Enforcing**。

**OS4 由小米官方 OTA 原包移植**，合并原厂系统分区与小米扩展，保留完整系统应用、原生桌面、首次开机引导及 Flutter 玻璃界面。天气、相册和渲染修复已预装，提供更完整的 HyperOS 软件体验。

## 版本选择

| | HyperOS 3 | HyperOS 4（推荐） |
| --- | --- | --- |
| 系统 | 3.0.2.0.WMCCNXM / Android 16 | 4.0.17.0.XFRCNXM / Android 17 |
| 来源 | 小米 13（fuxi）MysticGSI | 小米 18 Pro（hongkong）官方 OTA 原包 |
| 特点 | 已验证 GPS 的基础 GSI 适配 | 完整原厂系统组件；原生桌面、玻璃、天气与相册适配 |
| 默认资源 | 2 核 / 2.5 GiB | 4 核 / 4 GiB |
| ADB | `emulator-5566` | `emulator-5574` |
| 启动 | `Start-HyperOS.command` | `Start-HyperOS4-Official.command` |

两版独立保存镜像与用户数据，可同时安装。镜像在 [GitHub Releases](https://github.com/NEORUAA/HyperOS-AVD/releases) 分卷提供，勿混用不同 Release 的文件。

## OS4 截图

<table>
  <tr>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.21.27.png" width="220" alt="OS4 首次开机引导" /><br />首次开机引导</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.21.44.png" width="220" alt="OS4 语言设置" /><br />语言设置</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.23.03.png" width="220" alt="OS4 小米账号" /><br />小米账号</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.25.00.png" width="220" alt="OS4 设置完成" /><br />设置完成</td>
  </tr>
  <tr>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.26.08.png" width="220" alt="OS4 原生桌面与玻璃" /><br />原生桌面与玻璃</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.27.25.png" width="220" alt="OS4 KernelSU / Enforcing" /><br />KernelSU / Enforcing</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.27.44.png" width="220" alt="OS4 系统版本" /><br />系统版本</td>
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.28.03.png" width="220" alt="OS4 锁屏界面" /><br />锁屏界面</td>
  </tr>
</table>

<details>
<summary>OS3 截图</summary>

| 桌面 | KernelSU | 设置 | 系统版本 |
| --- | --- | --- | --- |
| <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.18.png" width="200" alt="OS3 桌面" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.39.png" width="200" alt="OS3 KernelSU" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.47.22.png" width="200" alt="OS3 设置" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.49.35.png" width="200" alt="OS3 系统版本" /> |

</details>

## 安装与启动

需要 **Apple Silicon Mac、Python 3**，以及 Android Studio SDK 中的 **Android Emulator / Platform-Tools**。首次安装建议预留 40 GiB，更新预留 60 GiB；使用预构建包无需下载手机 OTA、GSI 或编译工具。

1. 下载或克隆本仓库，再下载所选 Release 的 `manifest.json` 和全部 `.tar.gz.partNNN` 分卷，放在同一目录。
2. 在仓库目录执行（无需手动解压；安装器自动校验 SHA-256 并选择 OS3 / OS4）：

   ```sh
   chmod +x Setup.command Start-HyperOS*.command
   ./Setup.command --bundle /path/to/release/manifest.json
   ./Start-HyperOS4-Official.command  # OS3: ./Start-HyperOS.command
   ```

首次启动会创建空白数据、初始化 KernelSU 并安装管理器；等待终端显示 `HyperOS is ready` 后完成系统引导。之后可双击对应启动脚本，应用和数据会保留。安装更新前关闭对应版本的 AVD；脚本会拒绝端口冲突和其它工作区的 AVD。

## 兼容范围

OS4 的桌面手势、近期任务入口、Flutter 文字与玻璃、天气、相册及完整开机引导已验证；默认使用原厂 1120×2436 / 480 dpi，接通虚拟电源保持常亮，息屏显示默认开启并设为始终显示。应用商店更新后若再次缺少元素，按 [OS4 文档](docs/hyperos4.md#应用商店更新桌面后)刷新补丁。

完整原厂软件不代表手机硬件全兼容：相机、音频默认关闭，蓝牙及云服务未完整验证；查找设备状态查询已禁用以解除引导黑屏。部分后台缩略图仍可能空白。两版均为实验镜像，KSU root 域仍为 permissive，Google 登录与 Play Integrity 未验证。

[OS3 兼容说明](docs/compatibility.md) · [OS3 重建](docs/rebuilding.md) · [OS4 适配与重建](docs/hyperos4.md) · [发布说明](docs/releasing.md)

致谢：小米、MysticGSI、[Android Emulator / AOSP](https://developer.android.com/studio/run/emulator-commandline)、[KernelSU](https://github.com/tiann/KernelSU)。镜像保留 SDK 的 `NOTICE.txt`，第三方组件归各自权利人所有。
