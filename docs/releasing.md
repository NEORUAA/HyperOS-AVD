# Release 发布

本次准备 **手机 OS4 v0.2.2 / r3 Pre-release** 与 **Installer 1.2.0 正式 Release**。本地准备不包含 push、tag 或 GitHub 发布；Pad 独立版本线保持不变。

| 内容 | Tag | 类型 | 标题与正文 |
| --- | --- | --- | --- |
| 安装器 | `installer-v1.2.0` | 正式，可设 Latest | [HyperOS-AVD Installer v1.2.0](releases/installer-v1.2.md) |
| 手机 OS4 r3 | `v0.2.2-a17-hyperos4-hongkong-r3` | Pre-release | [HyperOS 4 · v0.2.2 · Apple Silicon · r3](releases/os4-r3.md) |
| Pad OS4 r1 | `pad-v0.1.0-a17-hyperos4-yingtian-r1` | 既有 Pre-release | [Pad 正文](releases/os4-pad-r1.md) |
| 手机 OS4 r2 | `v0.2.1-a17-hyperos4-hongkong-r2` | 既有 Pre-release | [r2 正文](releases/os4-r2.md) |

## 本地准备

审查 Git 改动，将固件移植、渲染 / 背屏修复、保数据安装器与 ext4 扩容修复、文档按 Conventional Commits 分类。镜像、OTA、SDK、缓存、日志、备份与个人用户数据不提交 Git。安装器 ZIP 包含安装指南，**文档与源码定稿后再打包**；冻结代码修改后须重新打包并核验收据。

以已完成固化的独立 r3 构建为输入，直接打包它的实际镜像；不从正在使用的 AVD 复制用户分区。以下命令只生成本地文件：

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check

HYPEROS_AVD_WORKSPACE="$PWD/work/os4-r3-build" \
  python3 scripts/package_release.py --variant os4-official \
  --version v0.2.2-a17-hyperos4-hongkong-r3
python3 scripts/package_installer.py

(cd releases/v0.2.2-a17-hyperos4-hongkong-r3 && shasum -a 256 -c SHA256SUMS)
(cd releases/installer-v1.2.0 && shasum -a 256 -c SHA256SUMS)
```

输出目录或空白 userdata 模板已存在时会拒绝覆盖。重做未发布附件需先检查并移走这次的生成物，或用 `--output` 创建新目录；不要改动已有公开 Release 或实例备份。打包预检核验系统 / vendor 的实际字节、原始签名、修复资源和原生库，附带空白 userdata、匹配启动代码及预编译库，最终用户无需 NDK 或 OTA。

新建 / 扩容用户分区需 e2fsprogs（`brew install e2fsprogs`），必须在活动数据改动前通过依赖检查。扩容验证须检查真实 ext4 容量、原有文件与文件系统 UUID；只核对配置或 QCOW2 虚拟大小不足以证明成功。

上传前还需：逐卷校验与独立解包，核对冻结源码 / 二进制收据；从分卷验证全新安装、OOBE 与冷启动；在独立 r2 数据副本中验证保数据升级及恢复。包校验、已有测试实例和从发布包启动是不同的验证结果。

## 附件

| Release | 本地目录 | 上传内容 |
| --- | --- | --- |
| Installer 1.2.0 | `releases/installer-v1.2.0/` | 安装器 ZIP、`install.sh`、`installer.json`、`SHA256SUMS`，4 件 |
| 手机 OS4 r3 | `releases/v0.2.2-a17-hyperos4-hongkong-r3/` | 5 个镜像分卷、`manifest.json`、`SHA256SUMS`，7 件 |

r3 分卷合计 6,800,845,749 bytes（约 6.334 GiB）；前四卷各 1536 MiB，第五卷 358,394,805 bytes，每件小于 2 GiB。逐件 SHA-256 见目录内清单。每个 Release 的附件各自上传，保持文件名，不混放旧版本。包内不含个人账号、序列号、ADB 密钥或当前用户分区。

## 验证记录

独立 r2 → r3 实例的多次冷启动中，20 个检查项全部通过；14 个保留字段与旧数据一致，含 38 项第三方包清单、Android ID、引导状态、模拟序列号、AOD / 常亮 / 色彩设置和两个用户文件探针。天气检查为应用私有目录内的探针文件，不能扩大为全部数据库、账号或云端会话验收。

r3 当前测试实例已通过全局 HWUI Vulkan、锁屏编辑预览修复、独立背屏及右侧边缘返回、双击息屏 / 唤醒与冷启动检查。最终主机回归共 473 项，472 通过、1 跳过；系统 / vendor 的 55 个签名、原生和资源文件预检通过。5 个分卷及 manifest 的 SHA-256 全部通过，归档内 91 个文件逐项校验通过；Installer ZIP 的 64 个清单文件与冻结源码一致。实际新装与启动结果见 [r3 发布验证](releases/os4-r3-validation.md)；运行日志与含个人标识的原始 JSON 不公开。

39 项存储测试覆盖真实离线 ext4 / QCOW2 扩容、失败回滚与中断恢复；另在独立真实 AVD 中重现“虚拟盘 32 GiB、文件系统约 6 GiB”。完整备份与维护启动后，实际解密设备和 ext4 超级块均为 32 GiB，冷启动的 `/data` 约 31 GiB；测试文件、Android ID、引导状态与模拟序列号保留。已加密磁盘继续增大虚拟容量（如 32 → 64 GiB）暂不支持，原有 32 GiB 容量修复不受此限制。

历史验证见 [r2](releases/os4-r2-validation.md) 与 [Pad r1](releases/os4-pad-r1-validation.md)。相机、云服务等限制以各版本正文为准。

## 远端发布

完成上述检查、审阅分类提交与附件后，才执行以下发布步骤；这些操作不属于本地准备：

1. Push 已审阅提交，创建 `installer-v1.2.0` 与 `v0.2.2-a17-hyperos4-hongkong-r3` tag。
2. 先发布 Installer 1.2.0，上传 4 件附件，设为正式 Latest，使公共 `install.sh` 指向新安装器。
3. 再发布手机 r3，粘贴对应正文、上传全部附件并勾选 Pre-release。Pad、r2 及 OS3 的 Release 保留。
4. 在公共 GitHub 页面核对附件件数 / 大小、curl 入口与菜单发现；安装器需能同时发现手机、Pad 和 OS3 的各自版本。

必须先让 Installer 1.2.0 可下载，再开放需它完成升级的 r3 镜像。r3 清单仅允许 r2 保数据迁移；旧相机桥接 revision 1 会阻止迁移，需按[安装指南](installing.md)先处理，不能删除兼容检查绕过。
