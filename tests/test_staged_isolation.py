"""DeepAI must import without loading DeepX/Blender runtime modules."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]

def test_deepai_starts_without_deepx_imports():
    import app, agents
    assert Path(app.__file__).resolve().is_relative_to(ROOT)
    assert Path(agents.__file__).resolve().is_relative_to(ROOT)
    forbidden = ("deepx", "agents.blender", "agents.blender_agent", "blender_bridge")
    assert not [name for name in forbidden if name in sys.modules]

def test_deepai_config_is_staging_local():
    import config
    assert Path(config.__file__).resolve().is_relative_to(ROOT)
    assert config.BASE_DIR.resolve() == ROOT
