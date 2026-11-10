"""Child process for one backtest: `python -m tdserver.research.runone <id>`."""
import sys

from ..context import build_context, quiet_http_logs
from ..settings import get_settings
from .runner import run_one

if __name__ == "__main__":
    quiet_http_logs()
    print(run_one(build_context(get_settings()), int(sys.argv[1])))
