from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/generate-sync", status_code=501)
async def generate_sync() -> None:
    raise HTTPException(status_code=501, detail="Not implemented")
