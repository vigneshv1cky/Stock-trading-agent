#!/usr/bin/env python3
"""CLI entry point for etf-sentiment-analyzer."""

import sys
import os

# Add parent directory so imports work both as package and standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import main

if __name__ == "__main__":
    main()
