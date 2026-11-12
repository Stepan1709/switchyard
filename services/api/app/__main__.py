"""Placeholder entry point: replaced by real processes (api, gateway, worker) in later stages."""

from app import __version__


def main() -> None:
    print(f"switchyard {__version__}")


if __name__ == "__main__":
    main()
