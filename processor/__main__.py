"""Allow ``python -m processor`` as well as the ``tvvp`` console script."""

from processor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
