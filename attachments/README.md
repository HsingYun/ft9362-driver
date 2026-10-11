# 附件

## Windows 驱动分析样本

[windows-5.2.69.24/](windows-5.2.69.24/) 原样保存用户提供的驱动包中的六个文件，供协议研究和移植复核。它们是编译后的二进制及安装文件，不是厂商源代码，也不参与 Linux 构建。

来源目录：`GPD DUO _Pocket 4_fingerprint_DriverV5.2.69.24_WHQL/drivers/a3a5351f-4abd-491a-8e3f-6a2dca7f069f/`。归档日期：2026-10-11。INF 版本：`10/28/2024,5.2.69.24`；主 DLL 内部版本字符串为 `5.2.66.23`，两者不混用。

| 文件 | 用途 |
| --- | --- |
| [FtWbioDriverUmdfV3.dll](windows-5.2.69.24/FtWbioDriverUmdfV3.dll) | UMDF 主驱动，USB 通信、初始化、图像与电源分析来源 |
| [FtWbioDriverUmdfV3.inf](windows-5.2.69.24/FtWbioDriverUmdfV3.inf) | 设备绑定、服务及安装配置 |
| [ftWbioEngineAdapter.dll](windows-5.2.69.24/ftWbioEngineAdapter.dll) | 生物识别引擎适配器，保留原包完整性 |
| [focalFpSrvcDeamon.exe](windows-5.2.69.24/focalFpSrvcDeamon.exe) | 设备管理辅助服务 |
| [FocalFpEventLog.dll](windows-5.2.69.24/FocalFpEventLog.dll) | 事件日志资源 |
| [ftwbiodriverumdfv3.cat](windows-5.2.69.24/ftwbiodriverumdfv3.cat) | 原始签名目录文件；归档不代表已验证签名链 |

在本目录执行校验：

```sh
(cd windows-5.2.69.24 && sha256sum -c SHA256SUMS)
```

摘要见 [SHA256SUMS](windows-5.2.69.24/SHA256SUMS)，分析中的样本大小及摘要也保留在 [Windows 移植参考](../docs/windows-reference.md)。主 DLL 同时服务 `0752` 与 `C652`，包含多个条件固件分支；附件归档不表示这些分支均适用于 FT9362。

## 权利说明

这些文件保留厂商原始内容及其中的版权、签名和安装信息，权利归原权利人所有，不适用工程代码的 LGPL-2.1-or-later 声明。本目录未附带可确认的独立再分发授权；本仓库不授予厂商文件的再分发许可。
