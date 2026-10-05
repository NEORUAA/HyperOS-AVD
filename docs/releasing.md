# Release 发布

安装器与镜像分别发版。本次已准备 **Installer 1.1.0 正式 Release** 与 **Pad OS4 v0.1.0 / r1 Pre-release**；本地准备不包含 push、tag 或 GitHub 发布。

| 内容 | Tag | 发布类型 | 正文 |
| --- | --- | --- | --- |
| 安装器 1.1.0 | `installer-v1.1.0` | 正式 Release，可设 Latest | [安装器正文](releases/installer-v1.1.md) |
| Pad OS4 r1 | `pad-v0.1.0-a17-hyperos4-yingtian-r1` | Pre-release | [Pad 正文](releases/os4-pad-r1.md) |
| 手机 OS4 r2 | `v0.2.1-a17-hyperos4-hongkong-r2` | Pre-release | [手机正文](releases/os4-r2.md) |
| OS3 r1 | `v0.1.0-a16-hyperos3-fuxi-r1` | Pre-release | 原首版 Release |

新标题分别为 **HyperOS-AVD Installer v1.1.0** 与 **HyperOS 4 Pad · v0.1.0 · Apple Silicon · r1**。Pad 独立使用 `pad-v*` 版本线，从 `pad-v0.1.0` 起，不占用手机的 `v*` 版本号。安装器查询全部 Releases，包括镜像 Pre-release，不依赖 GitHub Latest。

## 本地准备

先审查改动、运行对应测试，将 Pad 构建、共享修复、安装器、文档按 Conventional Commits 分类提交。镜像、OTA、SDK、缓存、日志与个人用户数据保留在 Git 忽略目录。

使用独立候选目录，不改正在使用的 AVD：

```sh
python3 scripts/prepare_release_image.py --variant os4-pad \
  --source work/os4-pad --output work/release-os4-pad-r1
HYPEROS_AVD_WORKSPACE="$PWD/work/release-os4-pad-r1" \
  python3 scripts/package_release.py --variant os4-pad \
  --version pad-v0.1.0-a17-hyperos4-yingtian-r1
python3 scripts/package_installer.py
```

已有目录时拒绝覆盖；需要重新打包则使用新候选目录或新 revision。候选保留实际镜像中的最终补丁，启用安全 ADB，附带空白 userdata。format 3 镜像冻结匹配的启动代码、KernelSU、macOS 色彩库、libhgl、天气 ANGLE 及相机桥接库与校验收据；首次启动自动初始化，无需用户提供 NDK 或 OTA。

```sh
(cd releases/installer-v1.1.0 && shasum -a 256 -c SHA256SUMS)
(cd releases/pad-v0.1.0-a17-hyperos4-yingtian-r1 && shasum -a 256 -c SHA256SUMS)
```

[Pad 验证记录](releases/os4-pad-r1-validation.md)区分当前测试 AVD、离线候选预检、分卷解包与真正从发布包启动的结果。本次 7 个镜像附件与 4 个 Installer 附件校验通过，独立解包检查通过；尚未从发布候选启动新 AVD。打包或校验通过不能代替首次启动、OOBE 和渲染验收。

## 上传附件

| Release | 本地目录 | 必须上传 |
| --- | --- | --- |
| Installer 1.1.0 | `releases/installer-v1.1.0/` | 安装器 ZIP、`install.sh`、`installer.json`、`SHA256SUMS`，共 4 件 |
| Pad OS4 r1 | `releases/pad-v0.1.0-a17-hyperos4-yingtian-r1/` | 5 个镜像分卷、`manifest.json`、`SHA256SUMS`，共 7 件 |

本次镜像分卷合计 **7664326640 bytes（约 7.138 GiB）**：前 4 卷各 1536 MiB，末卷 1221875696 bytes；每件小于 2 GiB。Installer ZIP 为 **193907 bytes**。逐件大小与 SHA-256 见各目录清单，不要只上传镜像分卷，也不要将两个 Release 的附件混放。

系统镜像 SHA-256：`bc21260cbbf26950ac3cf3a4b67ed3248674af1fe0ce17ec41b2175acfa1398c`。

上传前核对提交、正文与验证记录，之后再手动 push、创建对应 tag 和 Release。先上传 Installer 1.1.0 并设为 Latest，使 `releases/latest/download/install.sh` 可用；再上传 Pad 镜像并勾选 Pre-release。远端发布后核对附件完整性、公共安装入口及菜单中 Pad 镜像发现结果。当前尚未执行这些远端操作。

手机历史发布的构建流程及验证仍见 [OS4 r2 记录](releases/os4-r2-validation.md)。镜像系列或 Android 版本变化应新建实例；同系列升级也须先关闭目标 AVD，由安装器备份后执行。
