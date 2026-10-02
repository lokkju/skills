import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture
def env_for(tmp_path):
    def make(**extra):
        env = {
            "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
        env.update(extra)
        return env

    return make
