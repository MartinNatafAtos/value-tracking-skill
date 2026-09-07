import sys
from pathlib import Path

# Garantit que `scripts` est importable quel que soit le répertoire depuis
# lequel pytest est invoqué.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
