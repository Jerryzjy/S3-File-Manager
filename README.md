# S3 File Manager

Windows 桌面客户端，用于浏览 Cloudflare R2 存储桶和批量下载文件。支持 Windows 10 / 11（64 位）。

**[下载 Windows 安装包](https://github.com/Jerryzjy/S3-File-Manager/releases/download/v1.2.0-beta.4/S3FileManager-1.2.0-beta.4-windows-x64-setup.exe)** · [所有版本](https://github.com/Jerryzjy/S3-File-Manager/releases) · [更新记录](CHANGELOG.md)

![S3 File Manager 主界面](docs/images/s3-file-manager.png)

## 功能

- 保存并切换多个 R2 连接，浏览目录和搜索文件。
- 批量下载文件或目录，保留目录结构。下载前检查可用空间和本地文件冲突。
- 查看速度、进度和失败原因，支持暂停、续传和重试。
- 在应用内检查更新。可选用 Windows DPAPI 加密保存密钥。

## 安装与使用

1. 下载并运行 `*-setup.exe`。安装后从开始菜单打开 **S3 File Manager**。安装时也可选择创建桌面快捷方式。
2. 点击「添加存储桶」，填写 Cloudflare Account ID、Bucket 名称、Access Key ID 和 Secret Access Key。浏览和下载使用目标桶的只读权限即可。
3. 点击「测试连接」，再选「连接并保存」。选择文件或目录，确认保存位置后开始下载。

[Releases](https://github.com/Jerryzjy/S3-File-Manager/releases) 也提供便携 ZIP。安装版和便携版共用当前 Windows 用户的配置。卸载保留配置、任务和已下载文件。

## 开源

项目采用 [GPL-3.0](LICENSE)。源码运行、测试和构建见 [贡献指南](CONTRIBUTING.md)。[安全政策](SECURITY.md) · [第三方许可](THIRD_PARTY_NOTICES.md)

S3 File Manager is a Windows x64 client for browsing Cloudflare R2 and batch-downloading files with pause and resume.
