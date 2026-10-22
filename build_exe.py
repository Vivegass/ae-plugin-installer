r"""
Сборка «AE Plugin Installer.exe» (один файл, без консоли).

    .venv\Scripts\python.exe build_exe.py

Нужны зависимости из группы build: pip install -e ".[build]"
Готовый exe — в папке dist\.
"""

import sys
from pathlib import Path

from PIL import Image
from PySide6.QtGui import QGuiApplication

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"


def make_ico(path: Path):
    """Иконка exe из того же рисунка, что и логотип в окне; каждый размер рисуется отдельно,
    чтобы буквы на 16–32 px оставались чёткими."""
    from ae_plugin_installer.gui import make_logo_pixmap
    sizes = (256, 128, 64, 48, 32, 24, 16)
    images = []
    for s in sizes:
        png = path.with_name(f"app_icon_{s}.png")
        make_logo_pixmap(s).save(str(png), "PNG")
        images.append(Image.open(png))
    images[0].save(path, sizes=[(s, s) for s in sizes], append_images=images[1:])


def bundled_7zip_args():
    """7-Zip с машины сборки кладётся внутрь exe — тогда rar/7z открываются и там,
    где ни 7-Zip, ни WinRAR не установлены. Лицензия 7-Zip (LGPL + ограничение unRAR)
    это разрешает при условии, что её текст идёт вместе с программой."""
    from ae_plugin_installer.core import find_extractors
    seven = next((e for e in find_extractors() if e.kind == "7z"), None)
    if not seven:
        print("! 7-Zip не найден — exe соберётся без встроенного распаковщика")
        return []
    d = Path(seven.exe).parent
    files = [d / "7z.exe", d / "7z.dll", d / "License.txt"]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        print(f"! в {d} нет {', '.join(missing)} — exe соберётся без встроенного 7-Zip")
        return []
    print(f"Встраиваю 7-Zip из {d}")
    return ["--add-binary", f"{files[0]};7zip", "--add-binary", f"{files[1]};7zip",
            "--add-data", f"{files[2]};7zip"]


def main():
    BUILD.mkdir(exist_ok=True)
    app = QGuiApplication(sys.argv[:1])  # QPixmap требует экземпляр приложения
    ico = BUILD / "app_icon.ico"
    make_ico(ico)
    del app
    PyInstaller.__main__.run([
        *bundled_7zip_args(),
        str(HERE / "ae_plugin_installer" / "__main__.py"),
        "--name", "AE Plugin Installer",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--paths", str(HERE),
        "--icon", str(ico),
        "--distpath", str(HERE / "dist"),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        "--exclude-module", "tkinter",
        "--exclude-module", "PySide6.QtNetwork",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.QtQuick",
    ])


if __name__ == "__main__":
    main()
