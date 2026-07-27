"""本地 OCR：证件拍照 → 建档字段。

引擎用 macOS 自带的 Vision 框架，识别全程在本机内存里完成，图片不落盘、不出本机。
本模块分两层：
  - 解析层（纯函数，不 import Vision）：任何平台都能跑、能测
  - 引擎层（软导入 Vision）：缺依赖时 available() 返回 False，其余功能不受影响
"""
import datetime as dt
import re
import threading
from dataclasses import dataclass

from local_app.timeutil import today_str   # 统一北京时区口径，不用裸 date.today()

try:
    import objc
    import Quartz
    import Vision
    _VISION_OK = True
except ImportError:                     # 非 macOS，或没装 pyobjc
    _VISION_OK = False

# 身份证校验位算法（GB 11643）：前 17 位加权求和模 11 查表
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"

_ID_RE = re.compile(r"\d{17}[\dXx]")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")   # 两侧不许挨数字，否则会从身份证号里抠出假手机号
_CJK = r"一-龥"

# 证件右侧是人像和全息防伪膜，防伪膜必被识别成乱码（实测 "y回THNXS"）→ 地址只收左侧
_ADDR_RIGHT_LIMIT = 0.6

# 解码后的像素上限。手机拍照(4800万像素顶天)在此之下；挡的是"几十KB解压成上亿像素"的压缩炸弹
_MAX_PIXELS = 60_000_000


@dataclass
class TextBox:
    """一块识别出的文字。坐标为 Vision 原生归一化坐标：原点左下，y 向上。"""
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float


def _id_birthday(num: str) -> str:
    """从身份证号第 7–14 位取出生日期。校验位只管最后一位算得对不对，**不管日期本身成不成立**——
    13 月 40 日照样能过校验。这里按真日历验一遍，顺带挡掉未来的生日。不成立就返回空串。"""
    try:
        d = dt.date(int(num[6:10]), int(num[10:12]), int(num[12:14]))
    except (ValueError, IndexError):
        return ""
    day = d.isoformat()
    return "" if day > today_str() else day     # 生日不可能在今天之后


def id_card_valid(num: str) -> bool:
    """校验位验算。认错一位数字、或号码是编的，都会在这里露馅。"""
    if not re.fullmatch(r"\d{17}[\dXx]", num or ""):
        return False
    total = sum(int(c) * w for c, w in zip(num[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == num[17].upper()


def _tight(s: str) -> str:
    return re.sub(r"\s", "", s or "")


def _cy(b: TextBox) -> float:
    return b.y + b.h / 2


def _match(boxes, pattern):
    """第一个 tight 文本匹配 pattern 的块，连同 match 对象一起返回。"""
    for b in boxes:
        m = re.search(pattern, _tight(b.text))
        if m:
            return b, m
    return None, None


def _same_row_right(boxes, anchor):
    """与 anchor 同一横排、位于其右侧的块，按 x 排序。"""
    ay = _cy(anchor)
    hits = [b for b in boxes
            if b is not anchor and b.x > anchor.x and abs(_cy(b) - ay) < anchor.h * 0.6]
    return sorted(hits, key=lambda b: b.x)


def _parse_name(boxes):
    # 版式 A（真证实测）：标签和值粘成一块
    b, m = _match(boxes, rf"^姓名([{_CJK}·]{{1,8}})$")
    if m:
        return m.group(1)
    # 版式 B：标签单独一块，值在右侧同排
    b, _ = _match(boxes, r"^姓名$")
    if not b:
        return ""
    for h in _same_row_right(boxes, b):
        t = _tight(h.text)
        if t not in ("性别", "民族") and re.fullmatch(rf"[{_CJK}·]{{1,8}}", t):
            return t
    return ""


def _parse_address(boxes):
    addr, m = _match(boxes, r"^住址(.*)$")
    if not addr:
        return ""

    # 下边界：「公民身份号码」标签块和 18 位号码块未必同排，取更靠上的那条，
    # 否则号码会被当成地址续行拼进来
    label, _ = _match(boxes, r"^公民身份号码")
    num, _ = _match(boxes, r"\d{17}[\dXx]")
    bottom = [b for b in (label, num) if b]
    floor = max(_cy(b) for b in bottom) if bottom else 0.0
    ceiling = _cy(addr) + addr.h * 0.6   # 含与标签同排的首行（版式 B）

    parts = [(_cy(addr), m.group(1))]
    for b in boxes:
        if b is addr or b in bottom:
            continue
        if floor < _cy(b) < ceiling and addr.x <= b.x < _ADDR_RIGHT_LIMIT:
            parts.append((_cy(b), _tight(b.text)))
    return "".join(t for _, t in sorted(parts, key=lambda p: -p[0]))


def parse_patient_card(boxes):
    """OCR 文字块 → 建档字段。

    返回 {"fields": {...}, "confident": [...], "lines": [...], "warnings": [...], "kind": ...}
    fields 的键名与 POST /api/patients 完全一致；没解析出的键不放进去（前端才不会覆盖已填内容）。
    """
    lines = [b.text for b in boxes]
    flat = _tight("".join(lines))
    fields, confident, warnings = {}, [], []

    # 身份证号：认出这 18 位，生日和性别就是算出来的，不会错
    m = _ID_RE.search(flat)
    if m:
        num = m.group().upper()
        fields["id_card"] = num
        birthday = _id_birthday(num)
        if id_card_valid(num) and birthday:
            fields["birthday"] = birthday
            fields["sex"] = "男" if int(num[16]) % 2 else "女"
            confident += ["id_card", "birthday", "sex"]
        elif not id_card_valid(num):
            # 号码本身不可信，从它推的生日性别更不能填
            warnings.append("身份证号校验位不匹配，可能认错或号码有误，请核对")
        else:
            # 校验位只管最后一位对不对，不管日期本身合不合法：13月40日照样能过
            warnings.append("身份证号里的出生日期不成立，请核对")

    kind = "id_card" if ("公民身份号码" in flat or "居民身份证" in flat or "id_card" in fields) else "unknown"

    if kind == "id_card":
        name = _parse_name(boxes)
        if name:
            fields["display_name"] = name
        addr = _parse_address(boxes)
        if addr:
            fields["address"] = addr

    # 手机号：登记表、微信截图、就诊卡都靠这条。
    # 逐行找（不用拼平的 flat）：跨行拼接会把上一行的尾数字和下一行的头数字粘成"手机号"。
    # 且两侧不许挨着数字：身份证号少认一位校验位时是 17 位数字，从中任取 11 位很像手机号，
    # 而电话是建档必填项——填进去就是把假号码存进了患者档案。
    for line in lines:
        m = _PHONE_RE.search(_tight(line))
        if m:
            fields["phone"] = m.group()
            break

    return {"fields": fields, "confident": confident, "lines": lines,
            "warnings": warnings, "kind": kind}


class OcrUnavailable(RuntimeError):
    """本机没有可用的文字识别引擎。"""


# 框架能 import 不代表 Vision 真能跑（受限进程/不兼容运行时）。但一张问题图让 Vision 出错，
# 不等于引擎坏了——只有连续多次都失败，才判定引擎级故障熔断，之后 available() 转 False 隐藏入口。
# 任何一次成功都清零，免得偶发坏图把全院 OCR 搞停。
_RUNTIME_BROKEN = False
_CONSECUTIVE_FAILURES = 0
_BREAKER_THRESHOLD = 3
_BREAKER_LOCK = threading.Lock()


def available() -> bool:
    if not _VISION_OK:
        return False
    with _BREAKER_LOCK:
        return not _RUNTIME_BROKEN


def engine_name():
    return "macos-vision" if available() else None


def recognize(image_bytes: bytes):
    """图片字节 → [TextBox]。图片只在内存中处理，不写临时文件。"""
    global _CONSECUTIVE_FAILURES, _RUNTIME_BROKEN
    if not _VISION_OK:
        raise OcrUnavailable("本机不支持文字识别")
    with _BREAKER_LOCK:
        if _RUNTIME_BROKEN:
            raise OcrUnavailable("识别引擎暂不可用，请改用手工录入")
    # 识别跑在线程池的工作线程里，那儿没有 Cocoa 的自动回收池，原生对象只堆不放
    # (实测 200 次识别泄漏 22MB)。服务常年不重启，必须自己套一个回收池。
    try:
        with objc.autorelease_pool():
            boxes = _recognize(image_bytes)
        with _BREAKER_LOCK:
            _CONSECUTIVE_FAILURES = 0       # 成功一次就清零，偶发坏图不累积
            _RUNTIME_BROKEN = False
        return boxes
    except (ValueError, OcrUnavailable):
        raise                               # 图片本身的问题/已知不可用，不算引擎故障，也不计数
    except Exception as e:
        # 这次识别失败了(objc.error / RuntimeError / 任何原生异常)。单张图出错不代表引擎坏，
        # 只有连续 _BREAKER_THRESHOLD 次都失败才判引擎级故障熔断，隐藏入口。
        with _BREAKER_LOCK:
            _CONSECUTIVE_FAILURES += 1
            if _CONSECUTIVE_FAILURES >= _BREAKER_THRESHOLD:
                _RUNTIME_BROKEN = True
        # 统一转成 OcrUnavailable，路由才能一口气转成 503(而不是漏出 500)。
        raise OcrUnavailable("识别失败，请换张更清晰的图，或改用手工录入") from e


def _recognize(image_bytes: bytes):
    data = Quartz.CFDataCreate(None, image_bytes, len(image_bytes))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    if src is None or Quartz.CGImageSourceGetCount(src) < 1:
        raise ValueError("无法解析图片")

    # 先读元数据(不解码)，再决定要不要解码：8MB 限的是压缩后的字节，
    # 一张几十 KB 的图能解压成几亿像素撑爆内存(压缩炸弹)。
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
    px = (props.get(Quartz.kCGImagePropertyPixelWidth) or 0) * (props.get(Quartz.kCGImagePropertyPixelHeight) or 0)
    if px > _MAX_PIXELS:
        raise ValueError("图片尺寸过大")

    image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if image is None:
        raise ValueError("无法解析图片")

    # 手机竖拍的照片，旋转只记在 EXIF 里，像素是躺着的。不把方向告诉 Vision 就会认歪。
    orientation = props.get(Quartz.kCGImagePropertyOrientation, 1) or 1

    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_orientation_options_(
        image, int(orientation), None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"识别失败: {err}")

    boxes = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        top = cands[0]
        bb = obs.boundingBox()
        boxes.append(TextBox(
            text=top.string(), conf=float(top.confidence()),
            x=bb.origin.x, y=bb.origin.y, w=bb.size.width, h=bb.size.height,
        ))
    return boxes
