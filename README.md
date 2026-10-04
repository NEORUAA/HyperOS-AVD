# HyperOS-AVD

在 Apple Silicon Mac 的 Android Studio ARM64 模拟器中运行小米 HyperOS，支持 **ADB、KernelSU root、GPU 加速和全局 SELinux Enforcing**。

**OS4 由小米官方 OTA 原包移植**，合并原厂系统分区与小米扩展，保留完整系统应用、原生桌面、首次开机引导及 Flutter 玻璃界面。天气、相册和渲染修复已预装，提供更完整的 HyperOS 软件体验。

## 版本选择

| | HyperOS 3 | HyperOS 4（推荐） |
| --- | --- | --- |
| 系统 | 3.0.2.0.WMCCNXM / Android 16 | 4.0.17.0.XFRCNXM / Android 17 |
| 来源 | 小米 13（fuxi）MysticGSI | 小米 18 Pro（hongkong）官方 OTA 原包 |
| 特点 | 已验证 GPS 的基础 GSI 适配 | 完整原厂系统组件；原生桌面、玻璃、天气与相册适配 |
| 默认资源 | 2 核 / 2.5 GiB | 4 核 / 6 GiB（完整负一屏模糊建议 8 GiB） |
| 新安装入口 | 中英 TUI，自定义名称 / 端口 | 中英 TUI，自定义名称 / 端口 |

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
    <td align="center"><img src="screenshots/os4/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-03%20%E4%B8%8A%E5%8D%883.28.03.png" width="220" alt="OS4 全天候息屏" /><br />全天候息屏</td>
  </tr>
</table>

<details>
<summary>OS3 截图</summary>

| 桌面 | KernelSU | 设置 | 系统版本 |
| --- | --- | --- | --- |
| <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.18.png" width="200" alt="OS3 桌面" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.46.39.png" width="200" alt="OS3 KernelSU" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.47.22.png" width="200" alt="OS3 设置" /> | <img src="screenshots/os3/%E5%B7%B2%E7%B2%98%E8%B4%B4%202026-10-02%20%E4%B8%8A%E5%8D%886.49.35.png" width="200" alt="OS3 系统版本" /> |

</details>

## 安装与升级

需要 **Apple Silicon Mac、Python 3、Android Studio SDK 的 Emulator / Platform-Tools**。建议预留 60 GiB 加用户数据备份空间，无需 NDK 或手机 OTA。

下载独立正式 Release 的 [Installer v1.0.0](https://github.com/NEORUAA/HyperOS-AVD/releases/tag/installer-v1.0.0)，解压后双击 **`Install.command`**，选择中文 / English。ASCII 菜单同时展示 OS3 / OS4，自动下载并校验镜像，可自定义 **AVD 名称、RAM、存储和 CPU**，支持保数据升级和检查更新。OS4 默认 6 GiB / 32 GiB / 4 核。

安装器与镜像独立发版：安装器使用 `installer-v*` 正式 Release，镜像继续使用各自的 Pre-release。也可直接从源码运行：

```sh
git clone https://github.com/NEORUAA/HyperOS-AVD.git
cd HyperOS-AVD
chmod +x Install.command
./Install.command
```

**v0.2.0 升级：关闭目标 AVD → 安装器选“升级” → 选择旧实例和新版本。** 自动备份 userdata、QCOW2 和加密密钥后升级，保留应用与数据；失败可恢复。之后从安装器或实例目录的 `Start.command` 启动。

[中英安装 / 升级 / 恢复指南](docs/installing.md) · [安装器发布说明](docs/releases/installer-v1.md) · [OS4 v0.2.1 更新日志](docs/releases/os4-r2.md)

## 兼容范围

OS4 的桌面手势、近期任务入口、Flutter 文字与玻璃、天气、相册及完整开机引导已验证；默认使用原厂 1120×2436 / 480 dpi，接通虚拟电源保持常亮，息屏显示默认开启并设为始终显示。应用商店更新后若再次缺少元素，按 [OS4 文档](docs/hyperos4.md#应用商店更新桌面后)刷新补丁。

v0.2.1 修复 60 Hz 合成、macOS 色彩、渐进式模糊、小爱光效、返回压暗和连续播放无声。音频默认开启，后摄支持编辑虚拟场景；小米相机桥接可选，基础拍照可用，后摄录像仍掉帧。蓝牙及云服务未完整验证；查找设备状态查询已禁用以解除引导黑屏。部分后台缩略图仍可能空白。两版均为实验镜像，KSU root 域仍为 permissive，Google 登录与 Play Integrity 未验证。

[OS3 兼容说明](docs/compatibility.md) · [OS3 重建](docs/rebuilding.md) · [OS4 适配与重建](docs/hyperos4.md) · [发布说明](docs/releasing.md)

致谢：小米、MysticGSI、[Android Emulator / AOSP](https://developer.android.com/studio/run/emulator-commandline)、[KernelSU](https://github.com/tiann/KernelSU)。镜像保留 SDK 的 `NOTICE.txt`，第三方组件归各自权利人所有。
