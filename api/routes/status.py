from fastapi import APIRouter, HTTPException, Request

from ..errors import InfrastructureUnavailable

router = APIRouter()


@router.get("/status/{task_id}")
async def get_status(task_id: str, request: Request) -> None:
    if not request.app.state.db_ready:
        raise InfrastructureUnavailable("task database is unavailable")
    raise HTTPException(status_code=501, detail="Not implemented")
