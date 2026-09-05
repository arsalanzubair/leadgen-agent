import sys
from pathlib import Path

# Make `from src.state import ...` work without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
