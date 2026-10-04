# HyperOS 4 · v0.2.1 · Apple Silicon · r2

建议 Tag：`v0.2.1-a17-hyperos4-hongkong-r2`，保持 **Pre-release**。

这是 v0.2.0 之后的第二版 OS4 适配。系统仍为小米 18 Pro 官方 OTA 移植的 **4.0.17.0.XFRCNXM / Android 17**；本次更新兼容层，不升级手机系统基包。安装工具以独立正式 Release 发布。

## 自 v0.2.0 的变化

- **安装与升级**：配合独立 Installer v1.0.0，支持中文 / English ASCII 菜单、自动下载校验、自选名称与资源。v0.2.0 升级自动备份数据、QCOW2 和加密密钥，支持恢复 / 回滚。
- **流畅度与动画**：修正面板 60 Hz、合成器却以 20 Hz 渲染的问题；恢复原版桌面 SF 动画，撤掉此前造成动画差异的本地绕过。修复活动返回时压暗 alpha 不渐隐。
- **色彩与模糊**：自然色彩模式配合 macOS 窗口 / Metal 层 sRGB 标记，改善过饱和；恢复控制中心和主题商店的渐进式模糊。
- **小爱光效**：修复长按电源键时整屏黑底，保留原版白色边缘光效和渐隐。
- **设备与资源**：同步官方 hongkong 公开机型 / 构建身份；每份数据随机生成并持久保留小米样式的模拟序列号。默认 RAM 从 4 GiB 提高到 6 GiB，存储 32 GiB，保留负一屏小组件添加入口；完整背景模糊建议 8 GiB。
- **相机实验适配**：启用可编辑后摄虚拟场景与模拟前摄，修复摄像头编号、元数据和延迟预览尺寸。可选预编译小米相机桥接支持基础预览、前后切换、拍照和进入录像；无需用户安装 NDK。
- **音频**：默认开启 Mac 声音输出，修复连续播放遇到 PCM I/O 错误后持续无声的问题；同一流最多恢复重试一次，保留实际错误返回和录音路径。

保留首版的 Flutter 文字 / 玻璃 / 相册阴影、天气预装与 ANGLE、KernelSU / 全局 SELinux Enforcing、完整开机引导、常亮及全天候息屏适配。

## 安装 / 升级

下载独立正式 [Installer v1.0.0](https://github.com/NEORUAA/HyperOS-AVD/releases/tag/installer-v1.0.0)，解压后双击 `Install.command`，也可从源码运行。选择中文 / English 后，安装器会自动获取 OS3 / OS4 镜像，无需手动下载分卷。

已有 v0.2.0 用户：**关闭目标 AVD → 升级 → 选择旧实例 → 选择本版**。保留原安装路径、名称和已有存储大小；脚本自动保存用户数据与恢复备份。详细说明：[安装与升级](https://github.com/NEORUAA/HyperOS-AVD/blob/v0.2.1-a17-hyperos4-hongkong-r2/docs/installing.md)。

需要 Apple Silicon Mac、Python 3、SDK Emulator / Platform-Tools。建议预留 60 GiB 加备份空间。本镜像 Release 上传 5 个分卷、`manifest.json` 与 `SHA256SUMS`，保持文件名不变；安装器附件在独立正式 Release。

## 限制

相机仍是实验功能：后摄录像严重掉帧、部分算法和 Parrot HDR 未完整适配；麦克风默认关闭。部分后台缩略图可能空白；查找设备状态查询不可用，蓝牙、GPS 完整坐标回归、Google 登录、Play Integrity 和小米云服务尚未完整验证。未知应用原生库更新会拒绝修改，不能保证未来商店版本自动适配。

## English

v0.2.1 improves the compatibility layer for the same official Xiaomi 18 Pro HyperOS 4.0.17.0 / Android 17 base. It fixes 60 Hz rendering, stock launcher transitions, compositor fade alpha, macOS color matching, progressive blur, XiaoAI effects and sustained audio; adds editable virtual cameras and an optional precompiled Xiaomi camera bridge. The bilingual ASCII installer, automatic downloads and backed-up v0.2.0 upgrades are provided by the separate stable Installer v1.0.0 release.

Open `Install.command`, select English, then **Install** or **Upgrade**. Close the target AVD before upgrading; keep its name, path and storage size. Apps, encrypted userdata and completed setup are preserved. Camera remains experimental, especially rear video. See the bilingual installation guide for recovery and limitations.
