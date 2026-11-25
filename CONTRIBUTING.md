# Contributing to S3 File Manager

Issues, bug reports, feature requests, documentation updates, and pull requests are welcome. Please keep changes focused on the desktop client and include enough context for another contributor to reproduce and verify the behavior.

## Report an issue

Use [GitHub Issues](https://github.com/Jerryzjy/S3-File-Manager/issues) for bugs and feature requests. Include the Windows version, application version, steps to reproduce, expected behavior, actual behavior, and relevant log details. Remove access keys, secret keys, bucket contents, and other private data before attaching screenshots or logs.

Use the reporting channel in [SECURITY.md](SECURITY.md) for credential exposure, update integrity, or other security issues.

## Development setup

The supported development environment is Windows 10/11 x64 with Python 3.10 or newer.

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe client_launcher.py
~~~

The desktop application uses the current Windows user's application data directory. For isolated testing, set APPDATA to a temporary directory before starting the client.

## Tests

Run the test suite from the repository root:

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

The tests use local fixtures and mocked responses. They do not require a live Cloudflare R2 bucket or real credentials.

## Build the Windows packages

Build the portable executable and ZIP with PyInstaller:

~~~powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller cryptography
$env:Path = "$(Resolve-Path .\.venv\Scripts);$env:Path"
.\build-desktop.ps1
~~~

The portable output is written under dist/release/. To build the Windows installer, install Inno Setup 6.5 or newer and run:

~~~powershell
.\build-installer.ps1
~~~

Use -BuildDesktop when the executable has not been built yet:

~~~powershell
.\build-installer.ps1 -BuildDesktop
~~~

The setup file is named `S3FileManager-<version>-windows-x64-setup.exe` under `dist/release/`. For a tagged GitHub release, generate the manifest and source reference before building the installer. Replace `VERSION` with the version being built.

~~~powershell
python tools/package_release.py dist/release/VERSION --github-repository Jerryzjy/S3-File-Manager
.\build-installer.ps1
~~~

Verify installation, launch and uninstall before publishing. Upload the setup file, portable ZIP, versioned update EXE and manifest to the matching Release. Update `updates/windows-x64.json` on `main` after the versioned EXE is publicly downloadable.

## Pull requests

- Describe the user-visible behavior and the reason for the change.
- Add or update tests for behavior that can be verified without a live bucket.
- Keep user data and existing profile formats compatible when possible.
- Do not commit credentials, generated release packages, local application data, or personal screenshots.
- Include a screenshot or short recording when changing the desktop interface.

By submitting a contribution, you agree that it may be distributed under the repository's GPL-3.0 license.
