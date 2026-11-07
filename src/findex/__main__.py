"""python -m findex ... == findex ..."""

from findex.cli import app

if __name__ == "__main__":  # required for multiprocessing "spawn": children re-import __main__
    app(prog_name="findex")
