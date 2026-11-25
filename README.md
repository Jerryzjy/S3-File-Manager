<p align="center"><img src="docs/images/logo.png" alt="" width="72" height="72"></p>
<h1 align="center">S3 File Manager</h1>
<p align="center">浏览对象存储、批量传输文件和增量归档的 Windows 桌面客户端。</p>
<p align="center"><strong>简体中文</strong> · <a href="README.en.md">English</a></p>
<p align="center">
  <a href="https://github.com/Jerryzjy/S3-File-Manager/releases/tag/v1.3.0-rc.1"><img src="https://img.shields.io/badge/release-1.3.0--rc.1-4452CB?style=flat-square" alt="发布版本 1.3.0-rc.1"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-0078D4?style=flat-square" alt="Windows 10 和 11，64 位">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-465472?style=flat-square" alt="GPL-3.0 许可证"></a>
</p>
<p align="center">
  <a href="https://github.com/Jerryzjy/S3-File-Manager/releases/download/v1.3.0-rc.1/S3FileManager-1.3.0-rc.1-windows-x64-setup.exe"><strong>下载 Windows 安装包</strong></a> · <a href="https://github.com/Jerryzjy/S3-File-Manager/releases">所有版本</a> · <a href="CHANGELOG.md">更新记录</a>
</p>

![S3 File Manager 主界面](docs/images/s3-file-manager.png)

## 功能

- 保存并切换 Cloudflare R2、Backblaze B2 和自定义 S3 连接，浏览目录、搜索和批量下载。
- 批量下载文件或目录，保留目录结构。下载前检查可用空间和本地文件冲突。
- 增量归档会预览新增、变化或本地缺失的文件，并保留目录中的其他文件。
- R2 支持上传、分片续传、同名改名、桶内复制和限时下载链接。
- 在文件页展开传输队列，查看任务进度。传输页支持按批次暂停、继续、重试和导出失败清单。
- 在应用内检查更新和导出诊断。可选择用 Windows DPAPI 加密保存密钥。

## 安装与使用

1. 下载并运行 `*-setup.exe`。安装后从开始菜单打开 **S3 File Manager**。安装时也可选择创建桌面快捷方式。
2. 点击「添加存储桶」，选择存储服务，填写 Bucket、Access Key ID 和 Secret Access Key。R2 填写 Account ID。B2 填写桶页面上的 S3 Endpoint，使用 keyID 和 applicationKey。自定义 S3 填写 HTTPS Endpoint 和 Region。
3. 点击「测试连接」，再选「连接并保存」。选择文件或目录，确认保存位置后开始下载。
4. 文件页「更多操作」提供增量归档。选择文件或目录，确认保存位置和变更清单后开始。重复归档时选择同一范围和保存位置。
5. 对于 R2，使用有目标桶读取和写入权限的密钥可以上传或复制。上传窗口支持拖入文件和文件夹，默认跳过同名对象。64 MiB 及以上的文件支持分片续传。源文件需要保留到传输完成。
6. 选择一个 R2 文件，在「更多操作」生成限时下载链接，或将它复制为新名称。复制使用 R2 原生 S3 Endpoint，单文件上限约 4.995 GiB，并保留源对象及已存在的目标对象。

## 连接故障

浏览、下载和归档只需目标桶的读取权限。连接测试失败时，从服务商的桶页面核对 Endpoint、Region 和密钥权限。分片上传会话过期后可重新上传该文件。需要反馈异常时，可在侧栏导出诊断 ZIP。

[Releases](https://github.com/Jerryzjy/S3-File-Manager/releases) 也提供便携 ZIP。安装版和便携版共用当前 Windows 用户的配置。卸载保留配置、任务和已下载文件。

## 开源

项目采用 [GPL-3.0](LICENSE)。源码运行、测试和构建见 [贡献指南](CONTRIBUTING.md)。[安全政策](SECURITY.md) · [第三方许可](THIRD_PARTY_NOTICES.md)
