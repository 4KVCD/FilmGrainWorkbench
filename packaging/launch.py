"""Entry point of the packaged app: PyInstaller builds "Film Grain Workbench.exe" from this file."""

import sys

from afgs1wb.gui.app import main

sys.exit(main())
