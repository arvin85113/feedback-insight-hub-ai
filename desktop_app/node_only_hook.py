"""PyInstaller runtime hook for the isolated node-only distribution."""
import os

os.environ["FEEDBACK_HUB_NODE_ONLY"] = "1"
os.environ["DEPLOYMENT_MODE"] = "node"
