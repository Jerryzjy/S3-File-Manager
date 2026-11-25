# Security Policy

请通过 GitHub 的私密漏洞报告入口提交安全问题。请附上客户端版本、Windows 版本和可安全复现的简要说明。提交截图或日志前，请移除存储凭据、私密对象路径和带签名的下载链接。 [私密报告安全问题](https://github.com/Jerryzjy/S3-File-Manager/security/advisories/new)

普通使用问题、界面建议和功能请求请提交 GitHub Issue。 [提交 Issue](https://github.com/Jerryzjy/S3-File-Manager/issues/new)

## 维护版本与范围

维护当前最新的 GitHub 发布版本。检查范围包括 Windows 客户端、本机凭据存储、文件写入、断点续传、软件更新及 Windows 发布包。 [查看发布版本](https://github.com/Jerryzjy/S3-File-Manager/releases)

- 用户可启用 Windows DPAPI，将私有密钥加密保存给当前 Windows 用户。浏览和下载建议使用限定到目标桶的只读凭据。
- 云端对象名和本地任务数据会影响文件路径。客户端应将文件写入用户选择的目录，并明确提示同名和目录冲突。
- 更新仅在用户确认后安装。客户端通过 HTTPS 获取清单，核对文件大小与 SHA-256。更新的发布者身份依赖用户选择的 HTTPS 来源。

Please use GitHub private vulnerability reporting for security issues. Include the app and Windows versions and redact any credentials, private object paths and signed links. [Report a vulnerability](https://github.com/Jerryzjy/S3-File-Manager/security/advisories/new)
