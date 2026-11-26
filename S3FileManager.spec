# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import sys

from r2_batch_download import __version__

datas = [('r2_batch_download/preview_assets', 'r2_batch_download/preview_assets')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('qtawesome')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('boto3')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('botocore')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['client_launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'matplotlib', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    exe = EXE(pyz, a.scripts, [], name='S3FileManager', console=False, exclude_binaries=True, upx=False)
    collected = COLLECT(exe, a.binaries, a.datas, name='S3FileManager', upx=False)
    app = BUNDLE(
        collected,
        name='S3 File Manager.app',
        icon='r2_batch_download/preview_assets/s3-file-manager.icns',
        bundle_identifier='com.jerryzjy.s3-file-manager',
        version=__version__.split('-')[0],
        info_plist={'CFBundleDisplayName': 'S3 File Manager', 'CFBundleVersion': __version__.split('-')[0]},
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [], name='S3FileManager', console=False, upx=True,
        version='version-info.txt', icon='r2_batch_download/preview_assets/s3-file-manager.ico',
    )
