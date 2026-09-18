import sys
from pathlib import Path

# Make sure `import app...` resolves to this repo's app/ package regardless
# of how pytest is invoked (plain `pytest`, `python -m pytest`, from a
# different cwd, etc).
sys.path.insert(0, str(Path(__file__).parent))
