# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for VoiceBot.app
Build: cd voicebot && /opt/homebrew/bin/python3.10 -m PyInstaller --clean VoiceBot.spec
"""

import os
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None

# Path to the voicebot source directory (where this spec lives)
SRC = os.path.dirname(os.path.abspath(SPEC))

# MLX ships .metallib + .dylib in site-packages/mlx/lib/.
# mlx-whisper ships assets/mel_filters.npz.
# tiktoken_ext registers encodings via entry-points; we must bundle the
# subpackage explicitly or Whisper crashes with KeyError: 'gpt2' on
# first transcribe.
_mlx_datas = (
    collect_data_files('mlx_whisper')
    + collect_data_files('mlx')
    + collect_data_files('tiktoken_ext')
)
_mlx_bins = collect_dynamic_libs('mlx')
# mlx is a namespace package (no __init__.py); PyInstaller won't auto-walk
# its .py submodules, and the C extension imports mlx._reprlib_fix at load
# time — without this, mlx.core init crashes ModuleNotFoundError.
_mlx_submods = collect_submodules('mlx')

a = Analysis(
    [os.path.join(SRC, 'main.py')],
    pathex=[SRC],
    binaries=_mlx_bins,
    datas=[
        (os.path.join(SRC, 'assets'), 'assets'),
    ] + _mlx_datas,
    hiddenimports=[
        # PyObjC frameworks needed at runtime
        'AppKit',
        'Foundation',
        'CoreFoundation',
        'Quartz',  # CGEventPost for paste — see paster.py
        'objc',
        # rumps internals
        'rumps',
        # audio
        'sounddevice',
        '_sounddevice_data',
        # scipy
        'scipy.io',
        'scipy.io.wavfile',
        'scipy.signal',
        'scipy._lib.array_api_compat.numpy.fft',
        # PIL
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFilter',
        # MLX — explicit anchors; _mlx_submods below adds _reprlib_fix,
        # extension, _distributed_utils, optimizers and friends.
        'mlx',
        'mlx.core',
        'mlx.nn',
        'mlx.utils',
        *_mlx_submods,
        # mlx-whisper
        'mlx_whisper',
        'mlx_whisper.transcribe',
        'mlx_whisper.audio',
        'mlx_whisper.decoding',
        'mlx_whisper.load_models',
        'mlx_whisper.tokenizer',
        # Whisper tokenizer
        'tiktoken',
        'tiktoken_ext',
        'tiktoken_ext.openai_public',
        'regex',
        'more_itertools',
        'tqdm',
        'huggingface_hub',
        # stdlib
        'queue',
        'threading',
        'logging.handlers',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pynput', 'tkinter', 'matplotlib',
        # Heavy ML frameworks that mlx-whisper/scipy hooks might trigger.
        # Belt-and-suspenders: the build venv already won't have these,
        # but excluding them defends against module-scanning false positives.
        'torch', 'torchvision', 'torchaudio',
        'tensorflow', 'tensorboard',
        'google.protobuf',
        'IPython', 'jupyter', 'notebook',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VoiceBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VoiceBot',
)

app = BUNDLE(
    coll,
    name='VoiceBot.app',
    icon=os.path.join(SRC, 'assets', 'VoiceBot.icns'),
    bundle_identifier='com.mizz.voicebot',
    info_plist={
        'CFBundleDisplayName': 'VoiceBot',
        'CFBundleName': 'VoiceBot',
        'CFBundleShortVersionString': '2.0.0',
        'CFBundleVersion': '2.0.0',
        'CFBundleIdentifier': 'com.mizz.voicebot',
        'CFBundleExecutable': 'VoiceBot',
        'CFBundleIconFile': 'VoiceBot.icns',
        'CFBundlePackageType': 'APPL',
        # Menu bar app — no Dock icon
        'LSUIElement': True,
        # Required permission descriptions (macOS shows these in TCC dialogs)
        'NSMicrophoneUsageDescription':
            'VoiceBot needs microphone access to record your voice.',
        'NSAppleEventsUsageDescription':
            'VoiceBot needs this to paste transcribed text into other apps.',
        'NSPrincipalClass': 'NSApplication',
        # Retina
        'NSHighResolutionCapable': True,
        # Min macOS version (MLX requires macOS 13+)
        'LSMinimumSystemVersion': '13.0',
    },
)
