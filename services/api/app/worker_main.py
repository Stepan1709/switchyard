"""Entry point of the worker process: `python -m app.worker_main`."""

from app import db_models
from app.worker.consumers import build_consumers
from app.worker.runtime import main

# The worker writes rows whose foreign keys point at tables of modules it does not import by
# itself (calibration_runs -> projects). SQLAlchemy needs every table in the metadata to resolve
# them, so all models are registered here, as the migrations do.
assert db_models.ALL_MODEL_MODULES

if __name__ == "__main__":
    main(build_consumers)
