# 安装、升级与恢复

需要 **Apple Silicon Mac、Python 3、Android Studio SDK 的 Emulator / Platform-Tools**。预编译镜像无需 NDK 或原始 OTA；建议预留 60 GiB 加用户数据备份空间。新建或扩容用户分区需要 e2fsprogs，安装器会在改动前检查依赖：

```sh
brew install e2fsprogs
```

## 打开安装器

正式 Release 发布后，在终端运行：

```sh
curl -fsSL https://github.com/NEORUAA/HyperOS-AVD/releases/latest/download/install.sh | bash
```

选择中文 / English 和安装目录，安装器会自动下载、校验镜像。也可从 [Installer Releases](https://github.com/NEORUAA/HyperOS-AVD/releases) 下载 ZIP，解压后双击 `Install.command`，或从仓库运行：

```sh
git clone https://github.com/NEORUAA/HyperOS-AVD.git
cd HyperOS-AVD
chmod +x Install.command
./Install.command
```

**手机 OS4 r4 需要 Installer 1.2.1 或以上；r3 需要 1.2.0 或以上；Pad OS4 需要 1.1.0 或以上。** 安装器使用独立的 `installer-v*` 正式版本，镜像含 Pre-release，均可在菜单中发现。

**证书错误：** 源码安装器已修复 Python CA 环境导致的 `CERTIFICATE_VERIFY_FAILED`：仅在证书校验失败时，改用 macOS 系统 HTTPS 校验，保留证书、域名、文件大小与 SHA 校验；显式 CA 配置仍受尊重。已发布的 Installer 1.2.1 尚不包含此修复。

```text
+------------------------------------------------------------------+
| H Y P E R O S - A V D   /   INSTALLER 1.2.0                        |
+------------------------------------------------------------------+
| [1] Install a new AVD       [2] Upgrade / keep data                |
| [3] Start an instance       [4] RAM / storage / CPU                |
| [5] Browse images / updates [6] Recover / rollback                 |
| [7] Installer updates      [0] Exit                               |
+------------------------------------------------------------------+
```

| 镜像 | 默认 RAM / CPU / 存储 | 数据系列 |
| --- | --- | --- |
| 手机 OS4 | 6 GiB / 4 核 / 32 GiB | `hongkong`，r1 / r2 / r3 可直接保数据升 r4 |
| Pad OS4 | 4 GiB / 4 核 / 32 GiB | `yingtian`，与手机独立 |
| OS3 | 2.5 GiB / 2 核 / 32 GiB | `fuxi`，与 OS4 独立 |

资源可自定义，存储不允许缩小。Installer 1.2.0 会检查实际 ext4 容量，修复配置显示 32 GiB、系统 `/data` 仍只有原容量的问题；手机负一屏完整背景模糊建议 8 GiB。名称支持字母、数字、点、下划线和连字符。每个实例独立保存镜像、数据和端口，兼容修复使用实例配置与固件校验，不依赖出厂 AVD 名称。

首次安装使用空白用户分区，保留原版开机引导。按提示在虚拟机确认 ADB 授权，等待初始化完成。之后通过安装器“启动”或实例目录的 **`Start.command`** 启动，以加载 root、传感器、60 Hz、Mac 色彩等适配；Android Studio 的 Start 不包含全部初始化步骤。

手机小米相机桥接可选开启，Pad 自动初始化自身桥接；相机仍属实验功能。r3 会自动建立独立背屏窗口，支持原版右边缘向左返回、双击息屏与唤醒。

## r1 / r2 / r3 → r4 保数据升级

关闭目标 AVD，使用 **Installer 1.2.1** 选择“升级” → 原手机 OS4 实例 → `v0.2.3-a17-hyperos4-hongkong-r4`。自动备份用户分区、QCOW2 与加密密钥，校验固件与项目模块兼容性后切换到 OS4.0.18.0 修复镜像，保留名称、硬件配置、应用与设置；失败可从备份恢复。已有 r4 可重装，保数据降级不支持。

r4 修复已写入系统分区，新装或恢复出厂后也会保留。此前项目自带的 boot-service 测试模块与 r4 使用相同修复字节；本次升级不会删除用户模块。Installer 1.2.0 不识别 r4，请先更新安装器。r1 / r2 / r3 均可直接升级至 r4。

## r2 → r3 保数据升级

1. **关闭目标 AVD**，其他 AVD 可继续运行；打开 Installer 1.2.0。
2. 选择 `2 升级` → 已注册的 r2 实例 → `v0.2.2-a17-hyperos4-hongkong-r3`。保留实例名称、端口与存储容量。
3. 安装器先校验附件、保存完整备份，再短暂以无窗口方式启动旧 r2，检查版本、root 与模块，隔离旧版项目补丁；随后切换 r3 固件和匹配启动代码。
4. 按提示确认 ADB 授权；完成后从原实例的 `Start.command` 启动。首次升级可能需几分钟重建缓存，原应用、用户文件与已完成的引导保留。

备份包含 userdata 原始镜像、QCOW2 层及加密密钥，保存在实例的 `backups/`。确认升级正常后再自行清理。已有 AOD、常亮、色彩模式与模拟序列号保留；新用户默认值不会覆盖旧用户选择。

旧 r3 发布清单仅开放 **r2 → r3**；新 r4 清单采用通用向前升级策略，r1 / r2 / r3 可直接升 r4。OS3、手机与 Pad 不能互相覆盖。没有 `local/installed-release.json` 的早期源码实例可启动、调整硬件，但不能自动迁移固件。

**相机旧模块检查：** 若报 `XiaomiCamera revision 1 cannot migrate`，固件尚未切换。先使用“恢复 / 回滚”（若提示有待恢复事务），启动旧 r2，将项目的实验相机桥接更新至 revision 2 后关闭 AVD，再重试升级。安装器拒绝未知项目模块或被修改的固件；不会代替用户更改模块的禁用 / 删除选择。第三方模块仍由用户管理，不能据此保证兼容新固件。

## 调整存储

关闭目标 AVD，在菜单 `4 RAM / 存储 / CPU` 调整容量。未加密 ext4 在离线副本中检查、扩容并校验；原磁盘链保存在 `.userdata-resize-backup-*`，密钥与文件系统 UUID 保留。

旧版若虚拟盘已是 32 GiB，但 `/data` 只有约 6 GiB，在该菜单仍填 **32 GiB**。安装器先保存完整备份，再临时启动同一实例，检查解密后的 `/data` 超级块；必要时在线扩容并复核容量、UUID 与文件探针。随后关闭维护实例、记录容量证明，正常冷启动。r3 启动器也能自动执行这项旧盘检查。若提示 ADB 未授权，先在目标实例授权再重试。

已启用元数据加密的磁盘暂不支持继续增大虚拟容量（如 32 → 64 GiB），安装器会保留原数据并拒绝；可在新建实例时选择更大容量。32 GiB 虚拟盘在系统内通常显示约 31 GiB，格式化开销会减少可用空间。在线检查失败时备份仍保留；恢复必须先关闭目标实例。

## 恢复

配置切换失败会自动恢复。操作中断时，关闭目标 AVD，重新打开安装器，选择 `6 恢复 / 回滚`，也可选取升级前备份。回滚恢复当时的旧固件、配置和数据，备份后产生的数据不会并入旧备份。

安装器不会自动停止其他模拟器、缩小存储或保数据降级。启动超时也不会重置数据：系统仍在引导时先等待，确需重试则关闭目标 AVD 后冷启动。

## CLI / 离线安装

```sh
# A new phone instance, after r4 is published.
python3 scripts/manage.py install --variant os4-official \
  --root "$HOME/HyperOS-AVD/instances/My_OS4" --name My_OS4 \
  --release v0.2.3-a17-hyperos4-hongkong-r4 --ram 6 --storage 32 --cores 4 --start

# Offline install or upgrade: keep the existing root, name and port.
python3 scripts/manage.py install --root /path/to/instance \
  --name My_OS4 --bundle /path/to/r4/manifest.json \
  --ram 6 --storage 32 --cores 4

python3 scripts/manage.py releases --variant os4-official
python3 scripts/manage.py releases --variant os4-pad
python3 scripts/manage.py installer-updates
python3 scripts/manage.py list
python3 scripts/manage.py start --root /path/to/instance

# Pad defaults to 4 GiB RAM / 32 GiB storage / 4 cores.
python3 scripts/manage.py install --variant os4-pad \
  --root "$HOME/HyperOS-AVD/instances/My_Pad" --name My_Pad --release latest --start
```

离线安装需将同一 Release 的全部分卷与 `manifest.json` 放在同一目录，保持文件名不变；`SHA256SUMS` 用于额外核验。下载支持断点续传与 SHA-256 校验，失败保留缓存；API 限流或网络中断时可稍后重试，或使用 `--bundle`。

仅更改 Android Studio 的显示标签是安全的；手动修改 AVD ID 或数据目录名还需同步注册项与 `local/runtime.json`。
