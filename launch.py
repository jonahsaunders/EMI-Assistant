"""Dependency-free KiCad action: prepare the private environment, then open EMI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emi_assistant.bootstrap import main

if __name__ == "__main__":
    sys.exit(main())
