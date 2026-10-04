# 安装、升级 / Install & update

需要 Apple Silicon macOS、Python 3、Android Studio SDK 的 Emulator 和 Platform-Tools。无需 NDK、手机 OTA 或手动下载镜像分卷。建议预留至少 60 GiB；大容量用户数据备份还需要额外空间。

Requires Apple Silicon macOS, Python 3, and Android Studio SDK Emulator / Platform-Tools. No NDK, phone OTA, or manual firmware downloads. Reserve at least 60 GiB plus space for userdata backups.

## 启动安装器 / Open the installer

克隆仓库后双击 `Install.command`，或运行：

```sh
git clone https://github.com/NEORUAA/HyperOS-AVD.git
cd HyperOS-AVD
chmod +x Install.command
./Install.command
```

也可只下载独立正式 [Installer Release](https://github.com/NEORUAA/HyperOS-AVD/releases/tag/installer-v1.0.0) 的 `HyperOS-AVD-Installer-v1.0.0-macos-arm64.zip`，解压后双击 `Install.command`。**镜像会由安装器自动下载**；首次选择中文 / English，之后按菜单操作。

Clone the repository and open `Install.command`, or extract the small ZIP from the separate stable Installer Release. The installer downloads firmware automatically. Select 中文 / English, then follow the menu. Installer versions (`installer-v*`) are independent of firmware versions and prereleases.

```text
+------------------------------------------------------------------+
| H Y P E R O S - A V D   /   INSTALLER 1.0.0                      |
+------------------------------------------------------------------+
| Images: OS4 official OTA  |  OS3 GSI                             |
| Installer: stable releases  |  Images: prereleases included      |
+------------------------------------------------------------------+
+------------------------------------------------------------------+
| Dashboard                                                        |
+------------------------------------------------------------------+
| [1] Install a new AVD      [2] Upgrade / keep data               |
| [3] Start an instance      [4] RAM / storage / CPU               |
| [5] Browse images / updates [6] Recover / rollback               |
| [7] Installer updates      [0] Exit                              |
+------------------------------------------------------------------+
```

- 名称支持字母、数字、点、下划线、连字符；每个实例独立保存镜像、数据与端口。
- OS4 默认 6 GiB / 32 GiB / 4 核；负一屏完整背景模糊建议 8 GiB。OS3 默认 2.5 GiB / 2 核。已有存储只允许扩容。
- `[1]` 同时列出 OS3 / OS4 镜像及历史版本；选择页输入 `0` 返回。`[5]` 查看两版镜像更新，包含 Pre-release；`[7]` 单独检查正式安装器更新并给出下载链接。
- “启动”或实例目录的 `Start.command` 会启用 root、传感器、60 Hz 与 macOS 色彩适配。直接使用 Android Studio Start 不包含所有初始化步骤。
- 小米相机桥接为可选实验功能，安装时可开启，预编译附件无需 NDK；后摄录像仍有掉帧。

Names allow ASCII letters, digits, dots, underscores and hyphens. Instances have separate firmware, userdata and ports. OS4 defaults to 6 GiB RAM / 32 GiB storage / 4 cores; OS3 uses 2.5 GiB / 2 cores. Use 8 GiB for full OS4 App Vault wallpaper blur. Storage only grows. Install lists both image families; enter `0` to go back. Image update checks include prereleases; installer update checks list stable releases separately and provide a download link. Use the instance's `Start.command` for root and compatibility initialization. Xiaomi camera is an optional experimental bridge; rear video still drops frames.

## v0.2.0 保数据升级 / Keep v0.2.0 data

1. **关闭要升级的 AVD**，其余 AVD 可继续运行。
2. 打开新安装器 → `2 升级` → 选择已注册的 v0.2.0 实例 → 选择 v0.2.1 或后续兼容版本。
3. RAM / 核心可修改，存储应保持原值或增大。脚本校验全部分卷与安装文件后，备份完整 AVD 数据、QCOW2 和加密密钥，再切换固件与对应启动代码。
4. 启动后继续使用原应用、数据和已完成的开机引导。首次启动或升级后可能需要数分钟重建缓存，请等待引导完成。备份位于实例的 `backups/`，保留到确认升级正常。

Close only the target AVD. Choose **Upgrade**, select the registered v0.2.0 instance, then a compatible release. Downloads and extracted files are verified before a full AVD backup, including QCOW2 and encryption keys. Firmware and its matching startup code switch together; apps, userdata and completed setup remain intact. The first boot after installation or upgrade can take several minutes to rebuild caches. Keep `backups/` until satisfied.

安装器不会自动停止任何模拟器、缩小存储、降级已有数据或跨不兼容的 Android / 镜像族迁移。固件家族和加密模板不一致时，请安装独立 AVD。

The manager does not stop emulators, shrink disks, downgrade existing data, or migrate incompatible image families. Install a separate instance if the userdata family or encryption template changes.

最早从源码构建的 OS3 / OS4 也会显示在实例列表中；若没有 `local/installed-release.json`，可启动或调整硬件，但会拒绝自动迁移固件。上述保数据升级流程适用于从 Release 安装且带发布清单的实例。

Original source-built OS3 / OS4 workspaces are also listed. Without `local/installed-release.json`, they support start and hardware settings, but automatic firmware migration is refused. The upgrade flow above requires a release-installed instance with its manifest.

## 恢复 / Recovery

配置切换失败会自动恢复。若进程被中断，重新打开安装器，选择 `6 恢复/回滚`；也可用此入口选择升级前的备份。回滚包含旧镜像、配置和备份时的数据，之后产生的数据不会并入旧备份。仅在目标 AVD 关闭后操作。

Failed switches roll back automatically. After an interruption, use **Recover/Rollback**, or select the pre-upgrade backup explicitly. Rollback restores the old firmware, configuration and data as captured by that backup. Changes made after the backup are not merged. Close the target AVD first.

启动超时不会自动停止或重置 AVD。若系统仍在引导，先等待；确需重试时，关闭目标 AVD 后重新启动，保留用户数据。测试中曾遇到一次既有应用原生库读取等待，保数据冷启动重试后完成引导。

A boot timeout does not stop or reset the AVD. Wait if boot is still progressing; to retry, close only that AVD and start it again without resetting userdata. One test boot waited on an existing app's native library; a cold retry completed with data retained.

## 非交互 / CLI

```sh
# Latest OS4, automatically downloaded; use the same root/name when upgrading.
python3 scripts/manage.py install --root "$HOME/HyperOS-AVD/instances/My_OS4" \
  --name My_OS4 --release latest --ram 6 --storage 32 --cores 4 --start

# Offline install / upgrade from existing parts, preserving the target instance.
python3 scripts/manage.py install --root /path/to/old/work/os4-official \
  --name HyperOS_4_Official_API_37 --bundle /path/to/manifest.json \
  --ram 6 --storage 32 --cores 4

python3 scripts/manage.py releases
python3 scripts/manage.py releases --variant os3
python3 scripts/manage.py installer-updates
python3 scripts/manage.py list
python3 scripts/manage.py start --root /path/to/instance
```

下载支持断点续传与 SHA-256 校验；失败后保留缓存。GitHub API 限流或网络中断会显示错误，可稍后重试，或用离线 `--bundle`。同镜像族的未来版本沿用此流程；需要更新安装器格式时，脚本会明确提示。

Downloads resume and validate SHA-256. Failed transfers retain their cache. Retry API limits / network errors, or use offline `--bundle`. Future compatible releases follow the same flow; a newer required installer format is reported explicitly.
