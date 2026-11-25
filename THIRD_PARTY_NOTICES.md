# 第三方组件

本客户端使用下列组件。其版权和许可证归各自作者所有；licenses 文件夹保留组件分发元数据中的许可证文本。

| 组件 | 用途 | 许可证 |
| --- | --- | --- |
| Python | 运行环境 | PSF License |
| PySide6 / Shiboken6 / Qt | 图形界面 | LGPLv3 / GPLv3 / 商业授权（按具体组件条款） |
| QtAwesome / QtPy | 图标及 Qt 兼容层 | MIT |
| Material Design Icons | 界面图标 | Apache 2.0 |
| boto3 / botocore / s3transfer | S3 API 与下载 | Apache 2.0 |
| jmespath | SDK 数据处理 | MIT |
| python-dateutil | 日期处理 | Apache 2.0 / BSD |
| urllib3 | HTTP | MIT |
| packaging | 更新版本比较 | Apache 2.0 / BSD |
| PyInstaller | 打包 | GPLv2 with bootloader exception |

Qt 和 PySide6 使用动态库。发布包的 `licenses/` 提供 GPL-3.0、LGPL-3.0 和随依赖分发的许可文本。首次公开的 Windows 预发布版使用 Qt 和 PySide6 / Shiboken6 6.12.0。 [Qt 6.12.0 源码](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz) · [PySide6 / Shiboken6 6.12.0 源码](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz)

应用对应源码见发布包内的 `SOURCE.txt`。可以按仓库中的构建说明，从源码构建并替换所用 Qt 动态库。 [贡献与构建说明](https://github.com/Jerryzjy/S3-File-Manager/blob/main/CONTRIBUTING.md)
