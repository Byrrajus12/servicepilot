from fastapi import HTTPException, Request

MAX_SMS_REQUEST_BYTES = 16 * 1024


async def read_sms_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_SMS_REQUEST_BYTES:
            raise HTTPException(413, "SMS request is too large")
        body.extend(chunk)
    return bytes(body)
