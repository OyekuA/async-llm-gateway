from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/generate", status_code=501)
async def generate() -> None:
    raise HTTPException(status_code=501, detail="Not implemented")
