"""Allow `python -m src.propagation`."""
import sys

from .propagator import main

sys.exit(main())
