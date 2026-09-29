import os
import sys

# Make the plugin package importable as "RemoteWebControl" (the same name Cura uses).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
