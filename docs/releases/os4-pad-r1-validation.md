# Pad OS4 v0.1.0 / r1 发布验证

日期：2026-10-06。目标：`pad-v0.1.0-a17-hyperos4-yingtian-r1`，配套 Installer 1.1.0。仅准备本地文件和提交，未 push、打 tag 或发布。

## 已有测试镜像

以下是工作区 Pad 测试 AVD 的实际运行证据，不等同于从发布分卷安装新实例的验收。

| 项目 | 结果与范围 |
| --- | --- |
| 原厂配置 | `OS4.0.15.0.XBMCNXM / Android 17`；48 项公开身份与原包一致，`yingtian.xml` 哈希保持一致；硬件后端仍为 ranchu |
| 资源与方向 | 4 核；横屏 `3408×2272 / 400 dpi`；用户确认窗口正向，重新生成的近期任务缩略图正常 |
| 首次引导 | 用户要求重置测试实例后逐页完成原版 OOBE，跳过可选账号、密码与唤醒词录制；未永久关闭引导 |
| 新用户天气 | 原厂签名 APK 自动注册；上海预报、渐变背景、动态云层与不透明权限弹窗正常；进程实际加载匹配的 Flutter 与私有 ANGLE/MGL 库 |
| 冷启动 | Android ID、模拟序列号、引导状态、应用列表、共享文件清单、天气安装时间与定位权限保留，上海城市仍在 |
| 默认值 | 实际资源查询解析到 `screen_off_timeout=2147483647`；`sleep_timeout=-1`，启动器将供电常亮设为 `7`；按原机配置不启用 AOD |
| Root 与渲染 | KernelSU root、全局 SELinux Enforcing；HWUI 使用 Skia Vulkan；合成与物理刷新率均为 60 Hz，macOS sRGB 标记已接入 |
| 相机 | 前后摄预览、切换及基础 JPEG 曾实际测试；保存延迟未完全解决，录像未验收 |
| 小爱 | 冷启动后核验库挂载与加载路径；边缘白色光效出现并渐隐，桌面可见 |
| 截图 | README 的 3 张 Pad 截图为用户提供的原图，展示桌面、系统信息、KernelSU；不作为发布候选启动证明 |

## 本次发布候选

发布准备使用独立目录与空白 userdata，保留现有 Pad 用户分区。候选包含安全 ADB、最终优先级 `1000` 的 SettingsProvider overlay、匹配原生修复、冻结启动代码及全部预编译库。原 OTA、用户应用数据、备份与日志不进入公开包。

| 验证层 | 当前状态 |
| --- | --- |
| 源码与主机测试 | 最终完整测试集 214 项通过；`git diff --check` 通过 |
| 候选内容、签名及哈希 | 3 个实际打包分区、48 项原包身份、原机 XML、14 个签名 / 原生文件及启动 / 刷新 / 日志脚本全部通过预检；安全 ADB 为 `1`，debuggable 为 `0` |
| 默认值 overlay | 实际打包的 SettingsDefaults APK 经 aapt2 / apksigner 检查，V1 / V2 / V3 签名有效；优先级 `1000`，超时 `2147483647`、睡眠 `-1` |
| 预编译库 | 天气 ANGLE、相机桥接库与对应校验收据通过预检，无需用户 NDK |
| Installer 校验 | 1.1.0 最终 4 个附件全部通过 SHA-256 校验；ZIP 为 193907 bytes，54 个 payload 文件全部与当前源码清单一致 |
| 镜像分卷 | 5 卷及 manifest / SHA256SUMS 共 7 个附件全部通过 SHA-256 校验；分卷合计 7664326640 bytes（约 7.138 GiB） |
| 从分卷实际解包安装 | `setup.install_bundle` 在独立目录实际提取并校验 85 个文件；54 个冻结 runtime 文件与当前源码一致 |
| 无 NDK 路径 | 调用解包后的天气、相机与 macOS sRGB 库准备流程，均复用已核验的预编译产物，未执行 NDK 编译 |
| 发布候选新实例启动 / OOBE | 尚未验收；已有测试 AVD 的结果不能替代 |
| GitHub 下载与菜单发现 | 尚未发布，未进行远端验收 |

Installer ZIP SHA-256：`8617da05f1ece2a390fa234af3ff21a7b7828606a5a5a36cd2587c2636df992a`。系统镜像 SHA-256：`bc21260cbbf26950ac3cf3a4b67ed3248674af1fe0ce17ec41b2175acfa1398c`。本次归档分卷前 4 卷各 1610612736 bytes（1536 MiB），最后一卷 1221875696 bytes；manifest SHA-256：`b83f990c25c00aa87d7441d2298d5f03b9099cbaf6e79f8ab77cd941b1f62c9a`。逐件信息见打包目录清单。

## 已知限制

Pad 曾发生 MoltenVK descriptor 路径宿主崩溃及 goldfish_sync 内核异常；当前有规避配置，短期启动成功不能证明长期稳定。原厂相机后处理存在延迟保存，录像与完整旋转方向尚未验收。蓝牙、小米云服务、Google 登录及 Play Integrity 未完整验证，KSU root 域仍为 permissive。

当前测试镜像曾因 PackageManager 扫描前挂载天气原生库造成 native extraction `-18` 并删除天气私有数据。已将天气挂载推迟至系统启动完成、保持签名 APK 不变；随后用户授权重置，才完成上述新用户检查。该记录不声称历史天气私有数据已恢复。

本次验证未注册或启动新的 AVD，也未更改现有测试实例与用户数据。运行日志与逐项比较留在本地 Git 忽略目录，不作为公开附件。[发布流程](../releasing.md) · [Pad 适配记录](../hyperos4-pad.md)
