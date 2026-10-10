# HyperOS-AVD

在 Apple Silicon Mac 的 Android Studio ARM64 模拟器中运行小米 HyperOS，支持 **ADB、KernelSU root、GPU 加速和全局 SELinux Enforcing**。

**手机与平板 OS4 均由小米官方 OTA 原包移植**，保留原厂系统应用、桌面、首次开机引导及 Flutter 玻璃界面；OS3 使用 MysticGSI，适合体验基础功能。

## 版本选择

| | HyperOS 3 | HyperOS 4 手机 | HyperOS 4 Pad |
| --- | --- | --- | --- |
| 系统 | 3.0.2.0.WMCCNXM / Android 16 | 4.0.18.0.XFRCNXM / Android 17 | 4.0.15.0.XBMCNXM / Android 17 |
| 来源 | 小米 13（fuxi）MysticGSI | 小米 18 Pro（hongkong）官方 OTA | Xiaomi Pad 9 Pro Max（yingtian）官方 OTA |
| 显示 | 手机布局 | 主屏 1120×2436 / 480 dpi，背屏 912×596 / 450 dpi | 横屏 3408×2272 / 400 dpi |
| 默认资源 | 2 核 / 2.5 GiB | 4 核 / 6 GiB RAM / 32 GiB 存储（负一屏完整模糊建议 8 GiB RAM） | 4 核 / 4 GiB RAM / 32 GiB 存储 |
| 发布说明 | [兼容范围](docs/compatibility.md) | [r4](docs/releases/os4-r4.md) · [v0.2.4 / r5 准备中](docs/releases/os4-r5.md) | [r1](docs/releases/os4-pad-r1.md) · [v0.1.1 / r2 准备中](docs/releases/os4-pad-r2.md) |

Pad 使用独立的 `pad-v*` 版本线；手机与 OS3 的版本号继续沿用 `v*`。各版本独立保存镜像与用户数据，可同时安装。镜像在 [GitHub Releases](https://github.com/NEORUAA/HyperOS-AVD/releases) 分卷提供，勿混用不同 Release 的文件。

下一版手机 r5、Pad r2 与 [Installer 1.2.2](docs/releases/installer-v1.2.2.md) 正在本地准备，尚未发布。两版 OS4 共用 Core / Apps 两个自适应 KernelSU 模块和离线状态 WebUI；规则按实际内容与硬件能力匹配，保留用户开关和未知内容。RAM、CPU 与存储均可调整，Pad 默认 4 GiB RAM 不构成上限。

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

以下 4×2 为 r2 的界面参考，后续手机版保留这些功能。

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

待发布的手机 r5 / Pad r2 配套 Installer 1.2.2，新增 macOS 系统 HTTPS 证书校验回退，以及完整备份后的加密用户分区继续扩容；已发布的 1.2.1 尚不包含这两项修复。

安装器与镜像独立发版：安装器使用 `installer-v*` 正式 Release，镜像继续使用各自的 Pre-release。也可直接从源码运行：

```sh
git clone https://github.com/NEORUAA/HyperOS-AVD.git
cd HyperOS-AVD
chmod +x Install.command
./Install.command
```

**r3 → r4：关闭目标 AVD → Installer 1.2.1 选“升级” → 选择原实例与 r4。** 自动备份 userdata、QCOW2 和加密密钥，保留应用及个人设置，失败可恢复。r1 / r2 / r3 均可直接升级至 r4；已安装旧版实验相机模块时，先按安装指南处理兼容检查。Pad 与手机版不能互相覆盖升级；安装后从安装器或实例目录的 `Start.command` 启动。

[安装 / 升级 / 恢复指南](docs/installing.md) · [发布流程](docs/releasing.md)

## 兼容范围

**手机 OS4：** 保留全局 HWUI Vulkan、Flutter 玻璃、天气 / 相册、完整引导、60 Hz、Mac 色彩和声音适配，以及原版妙享背屏、独立窗口、双击息屏 / 唤醒和边缘返回；新用户默认常亮与 AOD。r5 候选针对已审计的手机 Vulkan 组合修复宿主命令序号忙等，桌面 CPU 中位由 117% 降至 26.7%；该有界对照结果不代表所有场景。

**Pad OS4：** 保留平板布局与原厂 `yingtian.xml` 配置；已核验横屏、天气、KernelSU 和冷启动，原机不支持的 AOD 不启用。Pad 未采用上述手机传输参数变更。

均为实验镜像：当前相机虚拟场景预览已复测，OEM / Parrot 拍照、HDR 与录像算法未完整验收。Pad 桌面 6325 尚未适配，FindDevice 20.15.70 缺少 APK，部分历史 Pad OOBE / Vulkan / Job 问题仍未闭环。未知可选 APK 会局部跳过以继续初始化，不保证其渲染或功能正常。蓝牙、小米云服务、Google 登录与 Play Integrity 未完整验证，KSU root 域仍为 permissive。

[OS3 说明](docs/compatibility.md) · [手机 OS4 适配](docs/hyperos4.md) · [Pad OS4 适配](docs/hyperos4-pad.md) · [补丁与验证边界](docs/os4-patches.md)

致谢：小米、MysticGSI、[Android Emulator / AOSP](https://developer.android.com/studio/run/emulator-commandline)、[KernelSU](https://github.com/tiann/KernelSU)。镜像保留 SDK 的 `NOTICE.txt`，第三方组件归各自权利人所有。
