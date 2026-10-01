# Release 命名与发布

每种系统镜像使用独立 Release，避免 Android 16 / HyperOS 3 与日后的 Android 17 / HyperOS 4 分卷混在一起。当前版本建议标记为 **Pre-release**。

## Tag 与标题

Tag 格式：`v<工具版本>-a<Android版本>-hyperos<主版本>-<源机型代号>-r<镜像修订号>`。

| 项目 | 当前推荐 |
| --- | --- |
| Tag | `v0.1.0-a16-hyperos3-fuxi-r1` |
| 标题 | `HyperOS 3.0.2.0 · Android 16 · Apple Silicon · r1` |
| 镜像版本 | `OS3.0.2.0.WMCCNXM` |
| 平台 | `macos-arm64` |

`v0.1.0` 表示项目工具版本；Android、HyperOS 和机型标识镜像来源；`r1` 表示该镜像的第一次发布。镜像或补丁更新时递增 `r2`、`r3`；工具更新时递增工具版本。完整 OEM 版本写在标题和正文中。

A17 / OS4 的未来命名模板：

```text
Tag:   v0.1.0-a17-hyperos4-CODENAME-r1
Title: HyperOS 4.x · Android 17 · Apple Silicon · r1
```

将 `CODENAME` 替换为实际源机型的小写代号，版本号按实际构建填写。这只是命名模板，当前脚本和验证仍针对 A16 / OS3；发布新镜像前需适配构建补丁、AVD 配置和 manifest 中的系统/API/KernelSU 等元数据，并重新验证。不同系统使用独立工作区、AVD 名称和 userdata。

## 可直接使用的首发正文

复制下方内容到 GitHub Release 描述：

````markdown
## 镜像信息

| 项目 | 版本 |
| --- | --- |
| HyperOS | 3.0.2.0.WMCCNXM |
| Android | 16 / API 36 |
| 来源 | 小米 13（fuxi）MysticGSI |
| 宿主平台 | Apple Silicon macOS / ARM64 |
| KernelSU | v3.3.0（32601），LKM |
| 项目工具 / 镜像修订 | v0.1.0 / r1 |

## 本次更新

- 支持官方 Android Studio ARM64 Emulator 原生启动。
- 提供安装、启动脚本及首次启动 KernelSU 自动初始化。
- 修复圆角、挖孔比例，以及复现到的 GNSS 回调死锁。
- 保持全局 SELinux Enforcing；KSU root 域仍为 permissive。

## 安装

下载本 Release 的 `manifest.json` 和全部 `.tar.gz.partNNN` 分卷，放在同一目录，按仓库 README 安装。无需手动解压；安装器会校验 SHA-256。

```sh
./Setup.command --bundle /path/to/release/manifest.json
./Start-HyperOS.command
```

## 验证与限制

已完成分卷导入、空白数据首次启动、KernelSU 初始化，以及两组坐标 / 四次 GPS 启停测试。

- 网络融合定位、小米云服务、Google 登录及 Play Integrity 未验证。
- 相机和音频默认关闭，蓝牙未验证。
- 首次设备设置曾出现一次锁屏杂志导致的 SystemUI 崩溃，随后恢复。
- Android Studio 内嵌窗口未验证；请先通过项目启动脚本运行。

实验版本，完整兼容范围见仓库 `docs/compatibility.md`。
````

后续版本沿用“镜像信息 → 本次更新 → 安装 → 验证与限制”，每次按实际镜像和测试结果改写。

## 上传文件

当前已生成的包位于 `releases/v0.1.0/`，请把以下五个文件上传到同一个 Release：

| 文件 | 大小 |
| --- | --- |
| `HyperOS-AVD-v0.1.0-macos-arm64.tar.gz.part001` | 1536 MiB |
| `HyperOS-AVD-v0.1.0-macos-arm64.tar.gz.part002` | 1536 MiB |
| `HyperOS-AVD-v0.1.0-macos-arm64.tar.gz.part003` | 约 471 MiB |
| `manifest.json` | 约 4 KB |
| `SHA256SUMS` | 小于 1 KB |

Release tag 可以采用上方推荐名称，现有文件名与 manifest 中的 `v0.1.0` 无需改动；安装器按 manifest 读取分卷。不要单独改分卷文件名，否则清单校验会失败。

后续生成新包时，让 `--version` 与 tag 一致，例如：

```sh
python3 scripts/package_release.py --version v0.1.0-a16-hyperos3-fuxi-r2
```

这条命令只改变包版本名称；镜像内容和元数据必须先完成更新。打包前停止本工作区的 AVD，避免镜像变动。

分卷默认 1536 MiB，低于 [GitHub 单个 Release 资源的 2 GiB 限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。所有分卷和 manifest 必须位于同一个下载目录，不能跨 Release 混用。

Git 只保存脚本、配置、文档及 README 截图；镜像、缓存和个人 AVD 数据保持忽略。固件包包含空白 userdata 模板，不打包现有 AVD 中的账号、应用或数据。
