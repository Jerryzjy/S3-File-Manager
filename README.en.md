<p align="center"><img src="docs/images/logo.png" alt="" width="72" height="72"></p>
<h1 align="center">S3 File Manager</h1>
<p align="center">A Windows desktop client for browsing object storage, transferring files and running incremental archives.</p>
<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>
<p align="center">
  <a href="https://github.com/Jerryzjy/S3-File-Manager/releases/tag/v1.3.0-rc.1"><img src="https://img.shields.io/badge/release-1.3.0--rc.1-4452CB?style=flat-square" alt="Release 1.3.0-rc.1"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-0078D4?style=flat-square" alt="Windows 10 and 11, 64-bit">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-465472?style=flat-square" alt="GPL-3.0 license"></a>
</p>
<p align="center">
  <a href="https://github.com/Jerryzjy/S3-File-Manager/releases/download/v1.3.0-rc.1/S3FileManager-1.3.0-rc.1-windows-x64-setup.exe"><strong>Download the Windows installer</strong></a> · <a href="https://github.com/Jerryzjy/S3-File-Manager/releases">All releases</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

![S3 File Manager browsing a bucket with the transfer queue open](docs/images/s3-file-manager.png)

## Features

- Browse and search Cloudflare R2, Backblaze B2 and custom S3 endpoints. Download files and folders in batches with pause and resume.
- Preview and download new, changed or locally missing objects with incremental archives. Other local files stay in place.
- Upload files and folders to R2 with multipart resume and automatic renaming on conflicts. Copy objects within a bucket or generate expiring download links.
- Check the transfer queue while browsing, retry failed items by batch and export a failure list or a diagnostics ZIP.
- Check for updates from the app. Windows DPAPI can encrypt saved secrets for the current Windows user.

## Install and connect

1. Run the `*-setup.exe` installer, then open **S3 File Manager** from the Start menu. The installer offers an optional desktop shortcut.
2. Add a bucket and enter its name and access keys. For R2, enter the Cloudflare Account ID. For B2, use the bucket's S3 endpoint, keyID and applicationKey. For a custom S3 provider, enter its HTTPS endpoint and region.
3. Test the connection, save it and select files or a folder to download. Read access is sufficient for browsing, downloads and archives. R2 uploads and copies also need write access.

Use **More actions** (更多操作) on the Files page for incremental archives, R2 copies and expiring links. Copies use the native R2 S3 endpoint and preserve the source and existing destinations. The per-object copy limit is approximately 4.995 GiB. R2 uploads of 64 MiB and above use multipart transfers.

The installer and portable ZIP use the same local settings. Uninstalling keeps connection settings, task records and downloaded files. For connection errors, check the endpoint, region and bucket permissions. You can export a diagnostics ZIP from the sidebar.

## Open source

Licensed under [GPL-3.0](LICENSE). See [Contributing](CONTRIBUTING.md) for running tests and building the app. [Security policy](SECURITY.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)
