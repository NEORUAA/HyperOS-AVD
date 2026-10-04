# Release 发布

安装器和镜像分别发版。本次已准备 **Installer v1.0.0 正式 Release** 与 **OS4 v0.2.1 / r2 Pre-release**，尚未打 tag 或发布。

| 内容 | Tag | 状态 | 正文 |
| --- | --- | --- | --- |
| 安装器 | `installer-v1.0.0` | 正式 Release | [安装器正文](releases/installer-v1.md) |
| OS3 r1 | `v0.1.0-a16-hyperos3-fuxi-r1` | Pre-release | 原首版 Release |
| OS4 r1 | `v0.2.0-a17-hyperos4-hongkong-r1` | Pre-release | [首版正文](releases/os4-r1.md) |
| OS4 r2 | `v0.2.1-a17-hyperos4-hongkong-r2` | Pre-release | [v0.2.1 正文](releases/os4-r2.md) |

安装器标题：**HyperOS-AVD Installer v1.0.0**，不要勾选 Pre-release，可设为 GitHub Latest。镜像标题：**HyperOS 4 · v0.2.1 · Apple Silicon · r2**，保持 Pre-release。两个版本号独立递增；安装器查询镜像使用全部 Releases，包含 OS3 / OS4 Pre-release，不依赖 GitHub Latest。

## 安装器附件

上传 `releases/installer-v1.0.0/` 的 **3 个文件**：

- `HyperOS-AVD-Installer-v1.0.0-macos-arm64.zip`：中英 ASCII TUI、安装管理代码与指南；不含镜像或用户数据。
- `installer.json`：安装器版本、平台、归档及源码校验信息。
- `SHA256SUMS`：ZIP 与元数据校验和。

本次 ZIP 为 **137259 bytes（约 134 KiB）**，包含 43 个可移植源码 / 配置 / 指南文件；已验证解压后的可执行入口。

```sh
python3 scripts/package_installer.py
cd releases/installer-v1.0.0
shasum -a 256 -c SHA256SUMS
```

安装器更新无需重发镜像。镜像 format 3 内仍保留与固件匹配的启动代码，启动时使用该冻结版本；安装器负责下载、校验和切换。

## OS4 r2 镜像附件

上传 `releases/v0.2.1-a17-hyperos4-hongkong-r2/` 的 **7 个文件**：5 个镜像分卷、`manifest.json`、`SHA256SUMS`。安装器 ZIP 已拆到独立正式 Release，不随镜像上传。

分卷合计 **6.315 GiB**：前 4 卷各 1536 MiB，最后一卷 338406247 bytes。manifest 为 19193 bytes，包含 format 3 固件、冻结启动代码、校验信息与数据兼容族。系统镜像 SHA-256：

```text
1d15b4e4b816eaf2a8832d7384244fdfc9f4eb99e3d06ab8efab953a1de7928a
```

保持文件名不变，勿跨版本混用。安装器核验全部分卷和解包文件；也可运行 `shasum -a 256 -c SHA256SUMS`。个人用户数据、备份、SDK、日志与 OTA 不在附件内。

## 镜像构建

使用独立发布候选，避免改动正在使用的 AVD：

```sh
python3 scripts/prepare_release_image.py --source work/os4-official --output work/release-os4-r2
HYPEROS_AVD_WORKSPACE="$PWD/work/release-os4-r2" \
  python3 scripts/package_release.py --variant os4-official \
  --version v0.2.1-a17-hyperos4-hongkong-r2
```

输出目录和模板已存在时拒绝覆盖。候选更新默认配置与小爱库，保留已验证 Vendor 相机、合成和 PCM 补丁；打包器核验实际内容并创建空白 userdata。可选小米相机工具位于 `tools/xiaomi-camera/`，需要配套校验清单。未来镜像要求 Installer 1.0.0；当前已验证的 r2 保持原清单和固件，不为 TUI 改动重新打包。

format 1 / 2 的旧 Release 仍可导入。format 3 将固件与启动代码一起版本化；兼容族不变且加密模板相同时可保数据升级，Android 或镜像族变化须新建实例。未发布的候选使用 `--bundle` 测试。

## 发布检查

审查 Git 改动与 [r2 验证记录](releases/os4-r2-validation.md)，提交对应源码后分别创建两个 tag，再上传各自目录的附件。不要只上传镜像分卷：manifest 与校验文件也必须上传。远端发布后核对安装器与镜像发现结果。README 的 4×2 OS4 截图是 r1 画面，未作为 r2 新截图。
