"""python -m ae_plugin_installer — окно установщика (с --worker — процесс установки с правами администратора)."""

import sys

# абсолютный импорт: этот же файл — точка входа собранного PyInstaller exe
from ae_plugin_installer.gui import main

if __name__ == "__main__":
    sys.exit(main())
