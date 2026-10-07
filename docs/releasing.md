# Release 发布

本次准备 **手机 OS4 v0.2.3 / r4 Pre-release** 与 **Installer 1.2.1 正式 Release**。只准备本地附件和分类提交，不 push、打 tag 或发布；Pad 与旧版 Release 保留。

| 内容 | Tag | 标题与正文 |
| --- | --- | --- |
| 安装器 | `installer-v1.2.1` | [HyperOS-AVD Installer v1.2.1](releases/installer-v1.2.1.md) |
| 手机 OS4 r4 | `v0.2.3-a17-hyperos4-hongkong-r4` | [HyperOS 4.0.18.0 · v0.2.3 · Apple Silicon · r4](releases/os4-r4.md) |

标题沿用当前 GitHub Release 的“HyperOS 系统版本 · 项目版本 · Apple Silicon · 修订号”格式；手机和 Pad 独立编号。

## 本地构建与验证

从已验收的 r3 **packed system 镜像**派生独立 r4 工作区，应用固定输入哈希的启动服务修复。不会复制 AVD 用户分区、账号、设备登记 ID 或个人密钥；空白 userdata 在打包时重新创建。

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
python3 scripts/prepare_release_image.py \
  --source /path/to/accepted-r3 --output work/os4-r4-build --boot-service-fixes
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-r4-build" \
  python3 scripts/package_release.py --variant os4-official \
  --version v0.2.3-a17-hyperos4-hongkong-r4
python3 scripts/package_installer.py
(cd releases/v0.2.3-a17-hyperos4-hongkong-r4 && shasum -a 256 -c SHA256SUMS)
(cd releases/installer-v1.2.1 && shasum -a 256 -c SHA256SUMS)
```

源工作区的 `local/build.json` 必须匹配实际 packed 镜像；准备器会重新解出 system / vendor 并核验原有修复，再重打包，避免使用已清理或过期的 raw 缓存。输出目录和空白 userdata 模板若存在会拒绝覆盖，重做时选新目录并保留用户备份。

上传前检查分卷哈希、逐文件解包哈希、冻结运行源码与安装器收据；用发布包验证新装及 r3 独立测试用户分区保数据升级；r1 / r2 和未来修订通过通用兼容规则测试，不重新下载旧镜像。验证边界见 [r4 验证记录](releases/os4-r4-validation.md)。

## 附件与提交

| Release | 本地目录 | 上传内容 |
| --- | --- | --- |
| Installer 1.2.1 | `releases/installer-v1.2.1/` | ZIP、`install.sh`、`installer.json`、`SHA256SUMS`，4 件 |
| 手机 OS4 r4 | `releases/v0.2.3-a17-hyperos4-hongkong-r4/` | 全部镜像分卷、`manifest.json`、`SHA256SUMS` |

每卷小于 2 GiB，文件名和件数以清单为准，不混入旧附件。Git 分类提交：启动服务 / 内核能力修复；r4 安装、升级与打包；发布说明与验证记录。镜像、日志、OTA、SDK、缓存及备份不提交。

## 后续发布顺序

1. 审阅本地提交与验证记录，随后 push 并为对应提交创建 `installer-v1.2.1`、`v0.2.3-a17-hyperos4-hongkong-r4` tag。
2. 先发布 Installer 1.2.1，使用上表标题和正文，上传 4 件附件，设为正式 Latest。
3. 再发布手机 r4，标题 **HyperOS 4.0.18.0 · v0.2.3 · Apple Silicon · r4**，上传全部附件并勾选 Pre-release。
4. 检查公共安装入口、附件件数与哈希，以及安装器菜单是否同时保留 OS3、手机和 Pad 的独立版本。

Installer 1.2.0 的 r3 清单校验不接受 r4，必须先让 1.2.1 可下载。r4 使用通用的同家族向前升级策略，支持 r1 / r2 / r3 直接升级；后续修订号无需逐版列出来源。固件哈希、加密模板、模块兼容性和版本方向仍需通过检查。不支持跨机型升级或保数据降级。历史结果见 [r3](releases/os4-r3-validation.md)、[Pad r1](releases/os4-pad-r1-validation.md)。
