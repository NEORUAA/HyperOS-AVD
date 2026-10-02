# Release 发布

OS3 / OS4 使用独立 Release。Tag 格式为 `v<工具版本>-a<Android版本>-hyperos<主版本>-<机型代号>-r<镜像修订号>`；镜像修订递增 `r1 / r2`，工具版本独立递增。

| | OS3 已发布 | OS4 首发 |
| --- | --- | --- |
| Tag | `v0.1.0-a16-hyperos3-fuxi-r1` | `v0.2.0-a17-hyperos4-hongkong-r1` |
| 标题 | HyperOS 3.0.2.0 · Android 16 · Apple Silicon · r1 | HyperOS 4.0.17.0 · Android 17 · 官方原包移植 · Apple Silicon · r1 |
| 来源 | fuxi MysticGSI | hongkong 官方 OTA |
| Manifest | format 1 | format 2，独立配置与公开构建信息 |

OS4 正文见 [os4-r1.md](releases/os4-r1.md)，可复制到 GitHub Release，建议勾选 **Pre-release**。后续版本沿用 Android / HyperOS / 机型 / 修订号命名。

## 打包

```sh
python3 scripts/package_release.py --variant os4-official --version v0.2.0-a17-hyperos4-hongkong-r1
```

OS3 使用 `--variant os3`（默认）。输出位于 `releases/<tag>/`，同名目录不会覆盖；本次未发布的 OS4 旧分卷已替换为最终配置的新包。打包器只读取固件白名单、AVD 模板和两个官方 KSU 运行附件，并创建全新空白 userdata。

OS4 打包前核验 packed / raw system 一致、生产版调试与安全 ADB 属性、小米桌面身份、开机引导与常亮配置、完整 hongkong.xml、AOD 服务、1120×2436 / 480 dpi，以及天气、相册、Flutter 和 ANGLE 文件哈希。打包后再次核对输入文件，发生变化时不会生成有效 manifest。

## 本次附件

目录：`releases/v0.2.0-a17-hyperos4-hongkong-r1/`。上传其中 **全部 7 个文件**：

| 附件 | 大小 |
| --- | --- |
| `.tar.gz.part001` — `.part004` | 每卷 1536 MiB |
| `.tar.gz.part005` | 320.06 MiB |
| `manifest.json` | 9988 bytes |
| `SHA256SUMS` | 五卷及 manifest 的 SHA-256 |

分卷合计 **6.31 GiB**，包含 21 个安装文件；文件名前缀为 `HyperOS-AVD-v0.2.0-a17-hyperos4-hongkong-r1-macos-arm64`。系统镜像 SHA-256：

```text
aac19ec5b339da3d1ee215db5adc9717a87a9b6d7995b838b210b60953d2ae30
```

分卷默认 1536 MiB，符合 [GitHub 单附件小于 2 GiB 的要求](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。保持附件名称，不跨版本混用；可以在附件目录运行 `shasum -a 256 -c SHA256SUMS` 手动核验。

## 发布前检查

28 项自动测试与 35 个镜像内签名 / 原生文件预检已通过。当前镜像已恢复出厂并完成首次启动，README 使用这次重新完成引导后截取的 8 张 OS4 图片。全部分卷与 manifest 校验、21 个文件的实际安装导入及空白 userdata 检查均已通过。

发布前提交对应源码、文档与截图，并在该提交上创建上述 tag，使 GitHub 自动提供的源码包含 OS4 安装器。镜像附件、个人 AVD 数据、环境与日志均被 Git 忽略。

附件导入校验在独立临时目录中进行，不注册或启动 AVD。设备功能验收与附件校验分开记录；尚未验证的硬件、云服务和新设置界面效果见 OS4 发布正文。
