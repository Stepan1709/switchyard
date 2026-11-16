from fastapi import APIRouter

from app.routes import analytics, auth, calibration, matrix, models, projects, root, rules

ROUTERS: list[APIRouter] = [
    auth.router,
    root.router,
    analytics.router,
    matrix.router,
    models.router,
    calibration.router,
    rules.router,
    projects.router,
]
