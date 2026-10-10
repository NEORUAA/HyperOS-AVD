# Pad OS4 v0.1.1 / r2 发布验证

日期：2026-10-10。目标 tag：`pad-v0.1.1-a17-hyperos4-yingtian-r2`；标题：**HyperOS Pad 4.0.15.0 · v0.1.1 · Apple Silicon · r2**。沿用官方 `OS4.0.15.0.XBMCNXM / Android 17`，配套 Installer **1.2.2**。当前仅本地准备，未 push、打 tag 或发布。

## 源码检查

| 项目 | 结果与范围 |
| --- | --- |
| 初次完整回归 | 后续发布准备与迁移修复前，共 923 项；867 项执行通过，56 项因专有输入不可用跳过；0 失败、0 错误。跳过不计为通过 |
| 后续发布准备改动 | 完整回归加载测试后修改了 `copy_release_inputs`；另跑 11 项受影响测试，10 通过、1 项可选 Flutter 产物跳过，不重复累加到上述 867 项 |
| 分类提交 | `1d3272b`：三版发行准备；`30ed818`：Pad 使用认证的共用应用产物；`4532cc7`：镜像准备后重新生成 Core 来源收据；`4697e82`：认证公开 r4 第 7 版 Flutter 模块迁移 |
| 后续共用迁移回归 | 52 项全部执行通过，0 跳过；历史规则精确认证，未扩大未知 / 自定义内容的自动替换范围 |
| 通用模块 | Core 4 / Apps 3 共用实现；Apps 使用同一完整预制包，Core 平台上下文绑定对应镜像；消费者离线使用冻结产物，无需 NDK、原 OTA 或生产者缓存 |

## 最终附件验收

以下项目必须使用最终分卷与冻结 Installer 1.2.2 完成，已有工作区结果不能代替。

| 项目 | 当前状态 |
| --- | --- |
| 分卷、manifest、SHA256SUMS、安装器附件 | 通过；5 卷 + manifest + SHA256SUMS 共 7 件，分卷合计 7,666,388,266 bytes；134 项归档文件逐项哈希与大小一致，每卷小于 2 GiB |
| 冻结 runtime 与通用模块 | 通过；冻结源码逐字节一致，Core 上下文绑定新 packed system；Apps 包与收据双端完全相同，完整 6 条内容规则通过认证 |
| 发布 userdata 与隐私 | 通过；重新创建的 6 GiB 空白模板只有空 lost+found，私人路径 / 密钥扫描通过；安装器按所选容量扩容，测试授权不进入发布模板 |
| 独立新装 | 通过；最终冻结 Installer 独立新装及冷启动，4 核 / 4 GiB RAM / 32 GiB，来宾 ext4 为 32,925,648 KiB；secure ADB=1、root / Enforcing、原厂三应用与稳定身份通过。RAM 可调整，无 4 GiB 上限 |
| 原厂配置与方向 | 附件 `yingtian.xml` 与配置哈希通过；来宾自然尺寸 2272×3408 / 400 dpi、保存旋转 3、原机 AOD 未启用。本轮 headless 未运行窗口对齐，实际仍为 rotation 0；未将此测试写成横屏 / 锁屏 / 近期任务视觉验收 |
| 来宾功能与实际加载 | Core 4 / Apps 3 只读状态收集通过；补丁核验后重开桌面与天气，Flutter / MGL / GL / EGL 的映射 inode 与输出 SHA 匹配。首次检查的天气旧进程仍持有原始 MGL，重开条件已记录；headless 未复测 WebUI 浏览器布局、相机、色彩与声音 |
| r1 → r2 实际保数据升级 | 本轮未测：公开 r1 资产不在本地，未下载。仅有通用兼容、备份与生命周期测试，不能替代此来宾路径 |

本轮发行 OOBE 自动测试使用 `--skip-oobe`，与逐页引导交互验收分开。正常启动和引导状态正确不代表每个引导页面已经验收。原有 Phone / Pad 仅正常关机以进行隔离 QA，原数据保留；现已通过正常可视启动恢复两实例，root / Enforcing、引导状态与三项原厂应用通过核验。三台临时测试实例、注册项、缓存及测试备份已清理，保留原 Pad 用户分区备份与最终附件。

manifest SHA-256：`29c281ea409716a2da7b7e4e6d61aa984d7762bf9485a644b4bc8660d9dc20c2`。Installer 共 4 件；ZIP 414,374 bytes，108 个源码文件及内部收据、权限、SHA256SUMS 均通过，SHA-256：`2b629451939499dcab0a4e2a2dffcf75e78cc459f02c11db2fd42c7a55c6343e`。

## 已有工作区证据

现有 Pad 已完成 Core 4 / Apps 3 正常升级与冷启动、天气相同签名 APK 保数据重装后的路径恢复，以及两模块状态 WebUI 检查。稳定身份、应用版本、抽样设置与功能选择保留；root、全局 SELinux Enforcing、HWUI `skiavk` 与 60 Hz 已核验。OEM 虚拟场景基础预览、LicenseActivity 正文显示与滚动通过；这些不是最终附件新装验收。详细范围见[补丁与运行证据](../os4-patches.md)。

Pad 关闭 `VulkanQueueSubmitWithCommands` 的试验未证明性能收益，已恢复原参数；Phone 的 117% → 26.7% 宿主 CPU 结果不适用于 Pad。当前短期测试不能证明历史宿主崩溃或退出问题已全部修复。

## 未闭环范围

- 反馈中的桌面 6325 尚无已审计规则；FindDevice 20.15.70 缺少 APK。未知可选内容局部跳过，不表示该更新功能正常。
- 部分历史 OOBE MoltenVK / 同步、Gallery 小部件与 FindDevice Job 问题尚未完成对应场景或根因闭环。
- OEM / Parrot 完整 JPEG、HDR 与录像算法尚未验收；Parrot 为可选功能，未安装不视为故障。
- 蓝牙、小米云服务、Google 登录与 Play Integrity 未完整验证；KSU root 域仍为 permissive，全局 Enforcing 未放宽。
- 本轮 headless Pad 已完成来宾正常 power-down，但宿主未在等待期内退出，清理时终止了受控测试进程；此退出问题未闭环。
- r1 → r2 实际保数据升级和长期宿主稳定性仍需单独验收。

原始日志、私人标识与测试 userdata 留在 Git 忽略目录，不进入公开附件。

[发布流程](../releasing.md) · [r2 Release 正文](os4-pad-r2.md) · [Installer 1.2.2 正文](installer-v1.2.2.md)
