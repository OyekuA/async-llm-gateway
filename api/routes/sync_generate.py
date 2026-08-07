import asyncio

from fastapi import APIRouter, HTTPException

from worker.llm import LLMBackendError, get_backend

from ..models import GenerateRequest, GenerateResult, StatusResponse

router = APIRouter()


@router.post(
    "/generate-sync",
    response_model=StatusResponse,
    response_model_exclude_none=True,
)
async def generate_sync(body: GenerateRequest) -> StatusResponse:
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: get_backend().generate(body.prompt, body.temperature, body.max_tokens),
        )
    except LLMBackendError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return StatusResponse(
        status="SUCCESS",
        result=GenerateResult(
            prompt=body.prompt,
            generated_text=result.generated_text,
            model=body.model,
            total_time_ms=result.total_time_ms,
            tokens_generated=result.tokens_generated,
            tokens_per_second=result.tokens_per_second,
        ),
    )
