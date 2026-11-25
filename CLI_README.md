# R2 批量下载工具

通过 Cloudflare R2 的 S3 兼容 API，将整个存储桶或指定目录前缀下的文件批量下载到本地。当前版本为命令行工具，适用于 Windows，也支持 Python 3.10+ 的其他系统。

## 1. 准备连接信息

需要四项配置：Cloudflare Account ID、R2 Access Key ID、对应的 Secret Access Key，以及 bucket（存储桶）名称。**只有 Access Key ID 无法认证。**

在 Cloudflare R2 的 API token 页面创建凭据，给予目标存储桶 **Object Read（对象读取）** 权限即可。无需公开 bucket。请在本地 PowerShell 中设置密钥，无需发到聊天里：

```powershell
$env:R2_ACCOUNT_ID = "your-cloudflare-account-id"
$env:R2_ACCESS_KEY_ID = "your-r2-access-key-id"
$env:R2_SECRET_ACCESS_KEY = "your-r2-secret-access-key"
$env:R2_BUCKET = "your-bucket-name"
```

默认 endpoint 为 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`。若 bucket 使用专属 jurisdiction endpoint，请额外设置 `$env:R2_ENDPOINT_URL` 为控制台提供的 S3 endpoint。也支持 `CLOUDFLARE_ACCOUNT_ID`、`AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY` 作为备用环境变量名。工具不会保存这些密钥；以上设置仅对当前 PowerShell 会话有效。

## 2. 安装

在本工具目录运行（当前机器已经完成安装）：

```powershell
python -m pip install -e .
```

安装后可在任何目录使用 `r2-batch-download`。若命令不在 PATH 中，可在工具目录通过 `python -m r2_batch_download.cli ...` 调用。

## 3. 检查并预览

检查配置是否齐全；此命令不联网，因此不代表密钥已通过服务器认证：

```powershell
r2-batch-download doctor
r2-batch-download --json doctor
```

列出前 20 个对象，验证实际连接、bucket 权限及前缀是否正确：

```powershell
r2-batch-download list --prefix "photos/" --limit 20
r2-batch-download --json list --bucket my-bucket --prefix "photos/"
```

## 4. 批量下载

先预览待下载列表，再执行下载：

```powershell
r2-batch-download download --prefix "photos/2026/" --out "D:\R2\photos" --dry-run
r2-batch-download download --prefix "photos/2026/" --out "D:\R2\photos" --workers 6
```

下载整个 bucket：

```powershell
r2-batch-download download --out "D:\R2\backup" --workers 4
```

| 参数 | 用途 |
| --- | --- |
| `--bucket my-bucket` | 覆盖环境变量中的 bucket |
| `--prefix "photos/"` | 只下载此前缀下的对象，保留对象目录结构 |
| `--out "D:\R2"` | 保存到指定本地文件夹 |
| `--workers 6` | 同时下载 6 个对象，默认 4 |
| `--limit 100` | 只处理前 100 个对象，默认遍历全部分页 |
| `--strip-prefix` | 从本地路径移除选定前缀；例如 `photos/a.jpg` 保存为 `a.jpg` |
| `--overwrite` | 替换已有文件 |
| `--dry-run` | 只预览对象，不写入下载目录 |
| `--json` | 放在子命令前，输出 JSON，便于其他程序调用 |

再次执行同一下载命令，会跳过本地大小与远端一致的已有文件。**这是大小比较，不是内容哈希校验，也不支持从单个文件的中间字节续传。** 大小不同的已有文件会报告失败，使用 `--overwrite` 可重新下载。目录占位对象会跳过。

下载先写临时文件，完成并检查大小后才移动到最终路径。SDK 提供网络失败重试；单个对象出错时继续处理其他对象，最后显示成功、跳过和失败数量。若有对象失败，退出码为 1。再次执行同一命令可重试失败项。

Windows 不支持的文件名字符会按百分号形式编码。`..` 等路径段会编码，输出路径不得越过下载根目录；指向根目录外的现有符号链接会被拒绝。多个对象若映射到同一个本地路径（例如仅大小写不同），或存在文件与目录名称冲突，后遇到的对象会报告失败。

## 程序输出

`--json` 模式下 stdout 输出 JSON，`doctor` 只显示凭据是否存在。常见结构：

- `list` / `download --dry-run`：`{ "bucket", "prefix", "count", "objects": [{"key", "size", "last_modified"}] }`，预览额外包含 `dry_run: true`。
- `download`：`{ "bucket", "prefix", "output", "total", "downloaded", "skipped", "failed", "results": [...] }`，每项包含 key、status 及 path/bytes 或 error。
- 命令错误：`{ "ok": false, "error": "..." }`，非零退出码。配置中的密钥值会从错误消息中脱敏。

## 验证状态

已进行离线逻辑测试和 SDK 配置检查。未提供真实 R2 凭据，尚未完成真实 bucket 的列举和下载验证。

官方参考：[R2 boto3 配置](https://developers.cloudflare.com/r2/examples/aws/boto3/) · [R2 API 凭据和权限](https://developers.cloudflare.com/r2/api/tokens/)
