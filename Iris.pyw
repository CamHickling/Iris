"""Iris Desktop Launcher.

Double-click this file to launch Iris without a console window.
The .pyw extension tells Windows to use pythonw.exe (no terminal).
"""

import os
import sys

# Ensure the project directory is the working directory
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
os.chdir(script_dir)

from gui import IrisApp

if __name__ == "__main__":
    app = IrisApp()
    app.mainloop()
