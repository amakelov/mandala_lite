# useful builtins
import sys
import argparse


def get_parser() -> argparse.ArgumentParser:
    """
    Copy of `get_parser` in c.py
    """
    parser = argparse.ArgumentParser()
    return parser


parser = get_parser()
args = parser.parse_args()

if __name__ == "__main__":
    from mandala_lite.all import *
    from mandala_lite.tests.test_stateful import *

    # setup_logging(level='info')
