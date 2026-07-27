"""拍照建档：证件图 → 建档字段。图片只在内存中识别，不落盘、不出本机、不写日志。"""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from local_app import ocr
from local_app.auth import require_perm

_MAX_BYTES = 8 * 1024 * 1024

# 请求准入从读取 body 前开始，并持续到解析结束。诊所单机并发很低，4 槽既容纳正常拍照突发，
# 又把内存中压缩图数量硬限制为 4；未获准请求不读取、不持有图片 body。
_REQUEST_LIMIT = 4

# 解码才是吃内存的大头(单张可到 6000 万像素)。诊所是单进程服务，几张大图并发解码就能把它撑死，
# 所以同时只让两张进解码。排队的请求只占着压缩字节(≤8MB)，等一下就好，不拒绝。
_DECODE_LIMIT = 2

_SLOTS_LOOP = None
_REQUEST_SLOTS = None
_DECODE_SLOTS = None


def _slots_for_current_loop():
    """Python 3.9 的 Semaphore 在构造时绑定 loop，因此首次请求时再为服务 loop 建槽。"""
    global _SLOTS_LOOP, _REQUEST_SLOTS, _DECODE_SLOTS
    loop = asyncio.get_running_loop()
    if _SLOTS_LOOP is not loop:
        _SLOTS_LOOP = loop
        _REQUEST_SLOTS = asyncio.Semaphore(_REQUEST_LIMIT)
        _DECODE_SLOTS = asyncio.Semaphore(_DECODE_LIMIT)
    return _REQUEST_SLOTS, _DECODE_SLOTS


def _sniff_image(data: bytes) -> bool:
    """按文件头判断是不是支持的图片。不靠扩展名（raw body 里没有文件名）。"""
    if data[:3] == b"\xff\xd8\xff":                       # jpeg
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":                  # png
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":     # webp
        return True
    if data[4:8] == b"ftyp":                              # heic/heif（iPhone 默认格式）
        return True
    return False


async def _read_limited(request: Request, limit: int) -> bytes:
    """流式读，边读边卡上限——不等整张图读完才拒，也不落临时文件。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=400, detail="图片不要超过 8MB")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=400, detail="图片不要超过 8MB")
    return bytes(buf)


def create_ocr_router():
    """OCR 拍照建档（GET /api/ocr/status、POST /api/ocr/patient-card）。无需数据库。"""
    router = APIRouter()

    @router.get("/api/ocr/status")
    def ocr_status():
        require_perm("patient.create")
        if not ocr.available():
            return {"available": False, "engine": None, "reason": "本机不支持文字识别"}
        return {"available": True, "engine": ocr.engine_name()}

    @router.post("/api/ocr/patient-card")
    async def recognize_patient_card(request: Request):
        require_perm("patient.create")   # 越权:读证件内容
        if not ocr.available():
            raise HTTPException(status_code=503, detail="本机不支持文字识别")

        request_slots, decode_slots = _slots_for_current_loop()
        async with request_slots:
            # 直接收原始图片字节：不走 multipart，避免 Starlette 把证件照 spool 到临时文件
            data = await _read_limited(request, _MAX_BYTES)
            if not data:
                raise HTTPException(status_code=400, detail="图片是空的")
            if not _sniff_image(data):
                raise HTTPException(status_code=400, detail="只支持 jpg/png/webp/heic 图片")

            started = time.perf_counter()
            try:
                async with decode_slots:    # 同时最多两张进解码，别让并发大图撑爆诊所服务
                    # 识别是同步的原生调用（数百毫秒），丢线程池跑，别卡住事件循环拖慢局域网其他人
                    boxes = await run_in_threadpool(ocr.recognize, data)
            except ValueError:
                raise HTTPException(status_code=400, detail="图片打不开，换一张试试")
            except ocr.OcrUnavailable as e:
                # 本机不支持、或引擎运行时挂了熔断——recognize 已把各种原生异常统一成这一类
                raise HTTPException(status_code=503, detail=str(e) or "本机不支持文字识别")

            result = ocr.parse_patient_card(boxes)
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            return result

    return router
