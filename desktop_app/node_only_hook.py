"""PyInstaller runtime hook: the packaged EXE is always the local node."""
import os

os.environ["DEPLOYMENT_MODE"] = "node"
