"""Put the tests directory on the path so one test module can reuse another's fixtures under importlib mode."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
