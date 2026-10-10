# Release 发布

本轮只准备 **手机 OS4 v0.2.4 / r5、Pad OS4 v0.1.1 / r2、Installer 1.2.2** 的本地附件与分类提交，不 push、打 tag 或发布。镜像使用 Pre-release，安装器使用正式 Release；两版官方基包继续为手机 4.0.18.0、Pad 4.0.15.0。

| 内容 | Tag | 标题与正文 |
| --- | --- | --- |
| 安装器 | `installer-v1.2.2` | [HyperOS-AVD Installer v1.2.2](releases/installer-v1.2.2.md) |
| 手机 OS4 r5 | `v0.2.4-a17-hyperos4-hongkong-r5` | [HyperOS 4.0.18.0 · v0.2.4 · Apple Silicon · r5](releases/os4-r5.md) |
| Pad OS4 r2 | `pad-v0.1.1-a17-hyperos4-yingtian-r2` | [HyperOS Pad 4.0.15.0 · v0.1.1 · Apple Silicon · r2](releases/os4-pad-r2.md) |

## 独立镜像与冻结运行包

从各自已核验的 **packed system 镜像**派生新的独立工作区。源 `local/build.json` 必须匹配实际镜像；准备器重新读取分区、核验默认值与启动策略，不信任过期 raw 缓存。只复制固件、工具和配置，不复制 `avd/`、账号、个人标识或密钥；打包器重新创建空白 userdata。已存在的输出或模板会拒绝覆盖，重做应使用新目录。

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
python3 scripts/prepare_release_image.py \
  --source /path/to/accepted-phone --output work/os4-r5-release-build \
  --variant os4-official
python3 scripts/prepare_release_image.py \
  --source /path/to/accepted-pad --output work/os4-pad-r2-release-build \
  --variant os4-pad
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-r5-release-build" \
  python3 scripts/package_release.py --variant os4-official \
  --version v0.2.4-a17-hyperos4-hongkong-r5 \
  --compat-cache-root /path/to/accepted-phone \
  --compat-cache-root /path/to/accepted-pad
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-pad-r2-release-build" \
  python3 scripts/package_release.py --variant os4-pad \
  --version pad-v0.1.1-a17-hyperos4-yingtian-r2 \
  --compat-cache-root /path/to/accepted-phone \
  --compat-cache-root /path/to/accepted-pad
python3 scripts/package_installer.py
```

安装器打包前确认源码版本与 bootstrap 元数据均为 **1.2.2**。两版携带共用的 Core 4 / Apps 3 实现、WebUI、冻结规则与历史认证资料；Apps 使用同一完整预制包，Core 的平台上下文分别绑定并核验对应镜像。生产者以显式、已认证的本地输入收集全部内容规则；缺少任一载荷应失败，不能按当前设备缩减通用包。用户消费附件不依赖 NDK、编译器、原始 OTA 或生产者缓存。

## 附件与验收

| Release | 本地目录 | 上传内容 |
| --- | --- | --- |
| Installer 1.2.2 | `releases/installer-v1.2.2/` | ZIP、`install.sh`、`installer.json`、`SHA256SUMS`，4 件 |
| 手机 OS4 r5 | `releases/v0.2.4-a17-hyperos4-hongkong-r5/` | 全部镜像分卷、`manifest.json`、`SHA256SUMS` |
| Pad OS4 r2 | `releases/pad-v0.1.1-a17-hyperos4-yingtian-r2/` | 全部镜像分卷、`manifest.json`、`SHA256SUMS` |

```sh
(cd releases/installer-v1.2.2 && shasum -a 256 -c SHA256SUMS)
(cd releases/v0.2.4-a17-hyperos4-hongkong-r5 && shasum -a 256 -c SHA256SUMS)
(cd releases/pad-v0.1.1-a17-hyperos4-yingtian-r2 && shasum -a 256 -c SHA256SUMS)
```

每卷小于 2 GiB，件数以清单为准。检查分卷、解包文件、预编译对象、冻结运行源码、WebUI 与安装器收据；核验空白 userdata、安全 ADB、原机配置和可启动基线。模块无法替代恢复出厂后的首轮系统扫描、Java / SELinux / 内核或宿主适配。

**最终附件的双端 headless 新装、冷启动、实际库加载与手机 r4 → r5 保数据升级已经验收；Pad r1 → r2 实际升级、逐页 OOBE及完整相机算法未测。Pad 来宾关机后宿主未自行退出的问题仍未闭环。** 后续验收继续使用最终附件，工作区结果不能代替对应路径；升级前后比较用户文件、包版本、稳定标识、设置和功能选择。加密分区扩容须保留完整离线备份，先核验磁盘字节，再测来宾解密 ext4 容量与文件探针；虚拟盘尺寸不能单独证明成功。TLS 回退须保留证书、域名与附件哈希校验。

详细运行证据及未闭环反馈见 [补丁记录](os4-patches.md)，发行包进度见 [手机 r5 验证](releases/os4-r5-validation.md)、[Pad r2 验证](releases/os4-pad-r2-validation.md)。不下载旧镜像补测，不把未复现、未提供 APK、可选跳过或静态认证写成已修复；发表前补齐对应发行包验收记录。

## 提交与后续发布

按维护规则分批提交：通用 Core / Apps 与生命周期；宿主性能 / 启动能力；安装器 TLS / 存储；发布元数据、文档与验证记录。镜像、原始日志、OTA、SDK、缓存、用户数据和备份不提交；仅清理已确认无引用的任务临时产物。

获得发布授权后，先核对最终提交和验收记录，再 push、创建上表 tag。先发布 Installer 1.2.2 为正式 Latest，再分别上传手机 r5、Pad r2 的全部附件，使用上表原样标题并勾选 Pre-release；最后检查公共安装入口、哈希和独立版本菜单。当前准备流程不执行这些远端步骤。

同系列旧修订通过通用向前兼容策略升级，修订号无需逐版枚举；仍须核验固件身份、加密模板、版本方向与模块所有权。未知或自定义内容不静默覆盖，手机、Pad、OS3 不能互相覆盖，保数据降级仍拒绝。历史记录保留：[手机 r4](releases/os4-r4-validation.md)、[Pad r1](releases/os4-pad-r1-validation.md)。
