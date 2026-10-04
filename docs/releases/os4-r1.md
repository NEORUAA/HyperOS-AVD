# HyperOS 4.0.17.0 · Android 17 · 官方原包移植 · Apple Silicon · r1

建议 Tag：`v0.2.0-a17-hyperos4-hongkong-r1`，勾选 **Pre-release**。

将小米 18 Pro（hongkong）的官方 OTA 原厂系统与小米扩展移植到 Android Studio ARM64 Emulator，保留完整系统应用、原生桌面、Flutter 玻璃和首次开机引导。与 OS3 的 MysticGSI 方案相比，OS4 提供更完整的原厂软件体验。

| 项目 | 配置 |
| --- | --- |
| HyperOS / Android | 4.0.17.0.XFRCNXM / Android 17（API 37） |
| 宿主 / 硬件底座 | Apple Silicon macOS / API 36 ARM64 ranchu、4 KB 内核 |
| KernelSU | v3.3.0（32601），LKM；全局 SELinux Enforcing |
| 默认 AVD | HyperOS_4_Official_API_37 / emulator-5574；4 核 / 4 GiB |
| 显示 / 机型配置 | 1120×2436、480 dpi；完整 hongkong.xml |

## 本次内容

- 预装天气与相册，修复 Flutter 元素、玻璃 / 模糊、相册阴影及天气图形兼容问题。
- 固化小米桌面 Quickstep 身份与手势适配，保留原厂系统应用签名。
- 保留完整开机引导，解除查找设备状态查询造成的黑屏。
- 使用原厂主屏分辨率与密度，默认开启息屏显示并设为始终显示，支持全屏 AOD。
- 关闭调试串口控制台，恢复生产版调试属性以修复开发者选项初始化异常。
- 默认接通虚拟电源并保持常亮；独立 OS4 数据目录和空白 userdata 模板，与 OS3 隔离。

## 安装

下载本 Release 对应的仓库源码，以及 **manifest.json 和全部 .tar.gz.partNNN 分卷**，放在同一目录，无需手动解压。本次共 5 个分卷，合计 **6.31 GiB**。`SHA256SUMS` 可用于手动核验，安装器也会自动校验全部分卷和安装文件。

需要 Apple Silicon Mac、Python 3，以及 Android Studio SDK 的 Android Emulator / Platform-Tools。首次安装建议预留 40 GiB，更新预留 60 GiB；预构建包无需 OTA、NDK 或 Build-Tools。

```sh
chmod +x Setup.command Start-HyperOS*.command
./Setup.command --bundle /path/to/release/manifest.json
./Start-HyperOS4-Official.command
```

等待终端显示 `HyperOS is ready` 后完成系统引导。后续双击 OS4 启动脚本即可；更新镜像前关闭对应 OS4 AVD，安装器保留用户数据。不要混用不同 Release 的分卷。

## 验证与限制

本次镜像已重新恢复出厂并完成启动，README 使用重置后重新完成开机引导的 8 张截图。桌面、天气、相册及手势的验收沿用此前已核验版本；开发者选项和 AOD 的界面效果由用户自行测试。

28 项自动测试、35 个镜像内签名 / 原生文件预检、全部分卷与 manifest 校验及 21 个文件的实际导入验证均通过；已确认 userdata 为空白模板。导入校验未注册或启动 AVD。

- 回桌面动画仍可能有延迟。
- 应用商店更新原生库后可能需要按 OS4 文档刷新补丁；未知 Flutter 引擎版本需另行适配。
- 查找设备状态查询不可用；蓝牙、Google 登录、Play Integrity 和小米云服务未完整验证。
- 相机、音频默认关闭；OS4 GPS 已合入补丁，尚未完成完整坐标回归。
- KSU root 域仍为 permissive；首次着色器初始化可能较慢。

这是实验预发行版本。完整原厂软件移植不等于手机硬件全兼容，详情见仓库 `docs/hyperos4.md`。
