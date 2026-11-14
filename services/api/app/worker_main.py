"""Entry point of the worker process: `python -m app.worker_main`."""

from app.worker.consumers import build_consumers
from app.worker.runtime import main

if __name__ == "__main__":
    main(build_consumers)
