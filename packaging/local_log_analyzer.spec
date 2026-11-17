# -*- mode: python ; coding: utf-8 -*-
# Local Log Analyzer — PyInstaller 설정 (계획서 §2.2, §11 Phase 9).
# 빌드: packaging\build.ps1  → dist\LocalLogAnalyzer\LocalLogAnalyzer.exe (폴더째 복사해 쓰는 onedir 방식)
# 데이터 파일(배포 기본 설정·파서 프로파일·DB 마이그레이션)은 코드가 찾는 상대 위치 그대로 넣는다.

from pathlib import Path

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "configs" / "settings.json"), "configs"),
    (str(ROOT / "configs" / "parser_profiles"), "configs/parser_profiles"),
    (str(ROOT / "app" / "storage" / "migrations"), "app/storage/migrations"),
    (str(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md"), "."),
    (str(ROOT / "docs" / "USER_GUIDE.html"), "docs"),
    (str(ROOT / "examples"), "examples"),
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=[],
    excludes=["tkinter", "pytest", "pytestqt", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalLogAnalyzer",
    console=False,  # 창 앱. 점검(--check) 결과는 <데이터>\logs\check.txt에도 남는다
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LocalLogAnalyzer")
