# Contributing to S3 File Manager

欢迎报告问题、提出功能建议或提交代码。当前开发重点是 Windows 上的 Cloudflare R2 浏览、批量下载、任务恢复及软件更新。 [开发路线图](PRODUCT_ROADMAP.md)

## 报告问题

请注明客户端和 Windows 版本、操作步骤、预期结果及实际结果。下载相关问题可以补充文件数量、大小范围、并发设置及是否可以稳定复现。你可以附上已脱敏的截图或日志。 [GitHub Issues](https://github.com/Jerryzjy/S3-File-Manager/issues)

涉及凭据、更新机制或潜在数据泄露的问题，请使用安全政策中的报告渠道。 [安全政策](SECURITY.md)

## 本地开发

使用 Windows 10 / 11 x64 和 Python 3.10 或更新版本。在仓库根目录运行以下命令。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe client_launcher.py
```

运行源码客户端会使用当前 Windows 用户的本机配置。需要做隔离验证时，可以在启动前临时将 `APPDATA` 指向一个独立的测试目录。

## 测试与提交

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用本地样本和模拟响应，无需真实 R2 凭据。涉及传输或恢复的改动，建议覆盖取消、磁盘空间、失败重试及已有断点。改动界面时，可在 PR 中附上相关页面的截图。

提交 PR 时，请说明具体要解决的问题、修改后的行为和验证方法。请保留已有用户配置兼容性，相关变更尽量集中在一个主题。公开示例请使用虚构的桶名和凭据，日志与截图先脱敏。

## Windows 构建

在开发环境安装打包工具，将虚拟环境的 `Scripts` 放在当前 PowerShell 进程的 PATH 首位后运行。

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller cryptography
$env:Path = "$(Resolve-Path .\.venv\Scripts);$env:Path"
.\build-desktop.ps1
```

输出位于 `dist/release/`，包括便携 ZIP、版本化 EXE、更新清单和 SHA-256。构建结果应在独立目录启动检查。旧版升级可用测试配置验证连接、设置和暂停任务是否仍可使用。

## 发布新版

1. 同步版本号与变更记录。版本分别位于 `r2_batch_download/__init__.py`、`pyproject.toml` 和 `version-info.txt`。 [更新记录](CHANGELOG.md)
2. 完成测试，运行 Windows 构建，再为 GitHub Release 生成使用绝对地址的更新清单。将下方版本替换为此次构建的版本。

   ```powershell
   .\.venv\Scripts\python.exe tools/package_release.py dist/release/1.2.0-beta.3 --github-repository Jerryzjy/S3-File-Manager
   ```

3. 使用对应源码的 `v<版本>` 标签。将便携 ZIP、`dist/release/updates/<版本>/` 下的 EXE 和 `update-windows-x64.json` 上传到该 GitHub Release。校验安装包大小与 SHA-256。
4. 发布后，核对清单引用的版本化 EXE 可公开下载。将构建生成的 `dist/release/updates/<版本>/update-windows-x64.json` 复制到源码的 `updates/windows-x64.json`，再提交并推送到 `main`。客户端使用这一固定地址检查更新。 [更新清单](https://raw.githubusercontent.com/Jerryzjy/S3-File-Manager/main/updates/windows-x64.json)
5. 从一个较早的版本验证「检查更新 → 下载并安装」。完成检查后，保留对应的 Release 标签和 EXE 以供下载。 [GitHub Release 资源链接](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)

更新清单可公开读取，只包含版本、下载地址、大小、校验值和发行说明。发布凭据由维护者保管。
