"""Allow `python -m src.visibility`."""
import sys

from .compute import main

sys.exit(main())
