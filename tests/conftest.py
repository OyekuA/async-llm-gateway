import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_temp_dir = tempfile.mkdtemp(prefix="async_llm_gateway_pytest_")
os.environ["DB_PATH"] = os.path.join(_temp_dir, "task_state.db")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
