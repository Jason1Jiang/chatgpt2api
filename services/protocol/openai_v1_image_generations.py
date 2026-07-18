from __future__ import annotations

import base64
from typing import Any, Iterator

from services.gemini_web_backend import (
    GEMINI_WEB_IMAGE_MODEL,
    GeminiWebError,
    ImageRequest,
    create_gemini_web_generation_backend,
)
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    count_text_tokens,
    format_image_result,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)
from utils.image_tokens import count_image_output_items_tokens, image_usage


def _gemini_error(error: GeminiWebError) -> ImageGenerationError:
    error_type = "server_error"
    if error.status_code == 400:
        error_type = "invalid_request_error"
    elif error.status_code == 429:
        error_type = "rate_limit_error"
    return ImageGenerationError(
        str(error),
        status_code=error.status_code,
        error_type=error_type,
        code=error.code.value,
    )


def _handle_gemini_web_generation(body: dict[str, Any]) -> dict[str, Any]:
    prompt = str(body.get("prompt") or "")
    n = int(body.get("n") or 1)
    if n != 1:
        raise ImageGenerationError(
            "gemini-web-image only supports n=1",
            status_code=400,
            error_type="invalid_request_error",
            code="unsupported_parameter",
            param="n",
        )
    if body.get("stream"):
        raise ImageGenerationError(
            "gemini-web-image does not support streaming",
            status_code=400,
            error_type="invalid_request_error",
            code="unsupported_parameter",
            param="stream",
        )

    response_format = str(body.get("response_format") or "b64_json")
    if response_format not in {"b64_json", "url"}:
        raise ImageGenerationError(
            "gemini-web-image supports response_format b64_json or url",
            status_code=400,
            error_type="invalid_request_error",
            code="unsupported_parameter",
            param="response_format",
        )
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    try:
        result = create_gemini_web_generation_backend().generate(
            ImageRequest(
                prompt=prompt,
                response_format=response_format,
                size=size,
                quality=quality,
            )
        )
    except GeminiWebError as error:
        raise _gemini_error(error) from None

    formatted = format_image_result(
        [
            {
                "b64_json": base64.b64encode(image.content).decode("ascii"),
                "revised_prompt": result.revised_prompt or prompt,
            }
            for image in result.images
        ],
        prompt,
        response_format,
        str(body.get("base_url") or "") or None,
    )
    formatted["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, GEMINI_WEB_IMAGE_MODEL),
        output_tokens=count_image_output_items_tokens(
            formatted.get("data"), size, quality
        ),
    )
    return formatted


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    quality = str(body.get("quality") or "auto")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None
    progress_callback = body.get("progress_callback")
    if model == GEMINI_WEB_IMAGE_MODEL:
        return _handle_gemini_web_generation(body)
    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        quality=quality,
        response_format=response_format,
        base_url=base_url,
        message_as_error=True,
        progress_callback=progress_callback,
    ))
    if body.get("stream"):
        return stream_image_chunks(outputs)
    result = collect_image_outputs(outputs)
    result["usage"] = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        output_tokens=count_image_output_items_tokens(result.get("data"), size, quality),
    )
    return result
