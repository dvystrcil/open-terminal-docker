"""
Pytest config. Sets the env vars `open_terminal.main` requires at import time
so that test modules can `from open_terminal.main import app, ...` without the
module's startup SystemExit firing.
"""

import os

os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key-not-used-by-handler")
