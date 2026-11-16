from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

router = APIRouter()


@router.get("/", include_in_schema=False)
async def index() -> Response:
    return RedirectResponse("/overview", status_code=303)
