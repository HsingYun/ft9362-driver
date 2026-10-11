# FT9362 驱动与采集工具

[English](README.md) | 简体中文

面向 FocalTech FT9362（USB `2808:0752`）的 Linux 开发工程，包含 libfprint 驱动、由 libfprint 动态链接的独立 C++ 算法库（`libfocaltech.0752.algo.so`）、Python 采集工具和协议研究文档。

**指纹身份认证功能正在进一步开发中，当前版本仅用于采集与开发，尚不可用于身份认证。** C++ 后端仍为占位实现：录入保存完整原始帧，认证在成功采集后返回匹配，识别对非空图库返回第一项。特征提取与实际指纹匹配尚未实现。

## 快速开始

在 Linux 上安装支持 flakes 的 Nix，从仓库根目录运行：

```sh
nix develop
python tools/capture.py
```

默认打开 GUI 并开始预览，不自动保存。GUI 支持暂停、按手指录制、追加录制和导出当前帧。终端采集与离线预览使用同一入口：

```sh
python tools/capture.py --cli --count 3 --timeout 30
python tools/capture.py --preview /path/to/frame.bin
```

默认输出到 Git 忽略的 `build/captures/`。开发环境不会自动安装宿主 udev 规则，也不会替换系统 libfprint；设备权限配置见[使用指南](docs/usage.md)。

## 工具界面

![Python 采集工具：离线预览合成条纹](docs/images/capture-gui.png)

离线模式界面，使用合成条纹演示，不含真实指纹数据。

## 工程结构

| 路径 | 内容 |
| --- | --- |
| [driver/](driver/) | libfprint C 驱动与不依赖 GLib 的 C++ 后端 |
| [tools/](tools/) | 统一 GUI / CLI 入口及内部模块 |
| [tests/](tests/) | 驱动、C ABI 和 Python 回归测试 |
| [docs/](docs/) | 使用、开发、协议及 Windows 移植参考 |
| [attachments/](attachments/) | Windows 驱动原始分析样本、来源和校验值 |
| [flake.nix](flake.nix)、[flake.lock](flake.lock) | 构建定义和锁定依赖 |

## 文档与开发

- [使用指南](docs/usage.md)：运行、录制、导出与故障排查。
- [开发指南](docs/development.md)：构建测试、状态机、内存所有权与 C++ 接口契约。
- [协议与数据格式](docs/protocol.md)：命令、原始帧、历史样本证据及未知项。
- [Windows 移植参考](docs/windows-reference.md)：按型号与分支核对的行为差异、命令和移植依据。

```sh
nix build
python tests/test_capture_tools.py
nix develop -c python tests/test_capture_gui.py
```

构建包含 C/C++ 测试；GUI 测试需要桌面显示服务，不访问传感器。硬件验证步骤见[开发指南](docs/development.md)。

## 致谢与许可证

本项目基于 **Ivan Kovalev** 的 [ft9362-driver](https://github.com/shokerplz/ft9362-driver)，感谢原作者的驱动实现与探索工作，以及 [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint) 项目提供的框架。

本分支截至 2026-10-11 的修改包括：原始帧采集状态机、独立 C++ 接口、GUI / CLI 采集与录制工具、回归测试、协议研究和工程结构整理。这些修改不代表原作者的上游实现。

代码继续采用 **LGPL-2.1-or-later**，保留原作者版权声明，许可证全文见 [LICENSE](LICENSE)。分发修改版本时请保留版权、许可证及修改说明；分发二进制还须按许可证提供相应源码及履行其他适用义务。软件按许可证所述不提供担保。[Windows 驱动附件](attachments/README.md)为厂商原始分析样本，其权利归原权利人，不适用本项目的 LGPL 声明；本仓库不授予其再分发许可。
