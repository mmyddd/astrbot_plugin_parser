import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")


class LimitedSizeDict(OrderedDict[K, V]):
    """
    定长字典
    """

    def __init__(self, *args, max_size=20, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: K, value: V):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)  # 移除最早添加的项


async def safe_unlink(path: Path):
    """
    安全删除文件
    """
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception:
        logger.warning(f"删除 {path} 失败")


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    """执行命令

    Args:
        cmd (list[str]): 命令序列
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        return_code = process.returncode
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

    if return_code != 0:
        error_msg = stderr.decode().strip()
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    target_path = output_path
    if output_path in (v_path, a_path):
        output_path = output_path.with_name(
            f"{output_path.stem}_merged{output_path.suffix}"
        )
    logger.info(f"Merging {v_path.name} and {a_path.name} to {output_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    if output_path != target_path:
        await safe_unlink(target_path)
        await asyncio.to_thread(output_path.replace, target_path)
        output_path = target_path
    cleanup = [p for p in (v_path, a_path) if p != output_path]
    await asyncio.gather(*(safe_unlink(p) for p in cleanup))
    logger.info(f"Merged {output_path.name}, {fmt_size(output_path)}")


async def merge_av_h264(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频，并使用 H.264 编码

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    logger.info(
        f"Merging {v_path.name} and {a_path.name} to {output_path.name} with H.264"
    )

    # 修改命令以确保视频使用 H.264 编码
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c:v",
        "libx264",  # 明确指定使用 H.264 编码
        "-preset",
        "medium",  # 编码速度和质量的平衡
        "-crf",
        "23",  # 质量因子，值越低质量越高
        "-c:a",
        "aac",  # 音频使用 AAC 编码
        "-b:a",
        "128k",  # 音频比特率
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name} with H.264, {fmt_size(output_path)}")


async def encode_video_to_h264(video_path: Path) -> Path:
    """将视频重新编码到 h264

    Args:
        video_path (Path): 视频路径

    Returns:
        Path: 编码后的视频路径
    """
    output_path = video_path.with_name(f"{video_path.stem}_h264{video_path.suffix}")
    if output_path.exists():
        return output_path
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    logger.info(f"视频重新编码为 H.264 成功: {output_path}, {fmt_size(output_path)}")
    await safe_unlink(video_path)
    return output_path


async def transcode_video_audio_to_lc(path: Path) -> Path:
    """把视频中非 AAC-LC 的音频转码为标准 AAC-LC，保持视频流 copy。

    用于解决 QQ 等平台因音频 profile（如 HE-AACv2）导致的视频无法正常渲染问题。
    """
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=profile",
            "-of",
            "default=nw=1:nk=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await probe.communicate()
        profile = (out or b"").decode(errors="ignore").strip()
    except Exception as e:
        logger.warning(f"[视频] ffprobe 探测失败，跳过转码: {e}")
        return path

    if not profile or profile.lower() in ("lc", "aac_lc", "aac lc"):
        return path

    out_path = path.with_name(f"{path.stem}_lc.mp4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-movflags",
            "+faststart",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except Exception as e:
        logger.warning(f"[视频] ffmpeg 调用异常，跳过转码: {e}")
        return path

    if proc.returncode != 0 or not out_path.exists():
        logger.warning(
            f"[视频] 音频转码失败(profile={profile})，改用原文件: "
            f"{(err or b'')[:200]!r}"
        )
        return path

    logger.info(
        f"[视频] 音频 {profile} -> AAC-LC 完成: {out_path.name} "
        f"({out_path.stat().st_size} bytes)"
    )
    await safe_unlink(path)
    return out_path


def fmt_size(file_path: Path) -> str:
    """格式化文件大小

    Args:
        video_path (Path): 视频路径
    """
    return f"大小: {file_path.stat().st_size / 1024 / 1024:.2f} MB"


def generate_file_name(url: str, default_suffix: str = "") -> str:
    """根据 url 生成文件名

    Args:
        url (str): url
        default_suffix (str): 默认后缀. Defaults to "".

    Returns:
        str: 文件名
    """
    # 根据 url 获取文件后缀
    path = Path(urlparse(url).path)
    suffix = path.suffix if path.suffix else default_suffix
    # 获取 url 的 md5 值
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    file_name = f"{url_hash}{suffix}"
    return file_name


URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def _iter_json_string_values(obj: Any):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_json_string_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_string_values(item)
    elif isinstance(obj, str):
        yield obj


def _clean_embedded_url(value: str) -> str:
    return unquote(value.strip().replace("\\/", "/"))


def extract_json_url(data: dict | str) -> str | None:
    """处理 JSON 类型消息段，提取可交给解析器处理的 URL。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    # 优先读取 QQ 卡片中明确表达跳转目标的字段。
    priority_paths = (
        ("miniapp", "legacyUrl"),
        ("miniapp", "pcJumpUrl"),
        ("miniapp", "jumpUrl"),
        ("miniapp", "sourceUrl"),
        ("miniapp", "url"),
        ("detail_1", "qqdocurl"),
        ("detail_1", "jumpUrl"),
        ("detail_1", "url"),
        ("detail_1", "sourceUrl"),
        ("detail_1", "shareUrl"),
        ("detail_1", "targetUrl"),
        ("news", "jumpUrl"),
        ("news", "url"),
        ("news", "sourceUrl"),
        ("news", "shareUrl"),
        ("music", "musicUrl"),
        ("music", "jumpUrl"),
        ("music", "url"),
    )
    for key1, key2 in priority_paths:
        node = meta.get(key1)
        if not isinstance(node, dict):
            continue
        value = node.get(key2)
        if not isinstance(value, str):
            continue
        match = URL_RE.search(_clean_embedded_url(value))
        if match:
            return match.group(0)

    # 部分 OneBot/QQ 卡片会把真实 URL 藏在更深层字段中。
    candidates: list[str] = []
    for value in _iter_json_string_values(data):
        cleaned = _clean_embedded_url(value)
        candidates.extend(match.group(0) for match in URL_RE.finditer(cleaned))

    return _pick_preferred_url(candidates)


# 分享卡优先匹配的域名，保持常见分享短链优先级。
_PREFERRED_URL_KEYWORDS = (
    "mobile.qzone.qq.com/l",
    "h5.qzone.qq.com/ugc/share",
    "m.qzone.qq.com",
    "user.qzone.qq.com",
    "qzone.qq.com",
    "b23.tv",
    "bili2233.cn",
    "bilibili.com",
    "xhslink.cn",
    "xhslink.com",
    "xiaohongshu.com",
)


def _pick_preferred_url(candidates: list[str]) -> str | None:
    """从候选 URL 中挑选最像分享跳转链接的一个。"""
    if not candidates:
        return None
    for keyword in _PREFERRED_URL_KEYWORDS:
        for url in candidates:
            if keyword in url:
                return url
    return candidates[0]


# QQ 官方机器人 ARK 卡片中可能存放跳转链接的键名（按可信度排序）。
OFFICIAL_CARD_URL_KEYS = (
    "jump_url",
    "qqdocurl",
    "jumpUrl",
    "qqDocUrl",
    "share_url",
    "shareUrl",
    "target_url",
    "targetUrl",
    "source_url",
    "sourceUrl",
    "pc_jump_url",
    "pcJumpUrl",
    "legacy_url",
    "legacyUrl",
    "url",
    "link",
    "href",
)

# 卡片里的封面/图标等资源链接，不是可解析的跳转目标。
OFFICIAL_CARD_NOISE_KEYS = frozenset(
    {
        "source_logo",
        "tag_icon",
        "preview",
        "icon",
        "avatar",
        "cover",
        "image",
        "thumb",
        "thumbnail",
    }
)


def _to_plain(value: Any) -> Any:
    """把 botpy 的消息对象/列表尽量转换成纯 dict/list，便于统一遍历。"""
    if isinstance(value, (dict, list, str)):
        return value
    if isinstance(value, tuple):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {
            key: _to_plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _iter_official_ark(
    data: Any, _seen: set[int] | None = None
) -> Iterator[dict[str, Any]]:
    """深度遍历原始消息负载，产出其中所有的 ARK 卡片节点。"""
    data = _to_plain(data)
    seen = _seen if _seen is not None else set()
    if isinstance(data, dict):
        ark = _to_plain(data.get("ark_data"))
        if isinstance(ark, dict) and id(ark) not in seen:
            seen.add(id(ark))
            yield ark
        is_ark_like = "ark_type" in data or "ark_name" in data
        if is_ark_like and id(data) not in seen:
            seen.add(id(data))
            yield data
        for value in data.values():
            yield from _iter_official_ark(value, seen)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _iter_official_ark(item, seen)


def _official_ark_candidates(ark: dict[str, Any]) -> list[str]:
    """从单个 ARK 卡片节点中提取候选跳转 URL。"""
    candidates: list[str] = []

    def collect(value: Any) -> None:
        """收集字符串（含嵌套 dict/list）里出现的所有 URL。"""
        if isinstance(value, str):
            cleaned = _clean_embedded_url(value)
            candidates.extend(match.group(0) for match in URL_RE.finditer(cleaned))
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in OFFICIAL_CARD_NOISE_KEYS:
                    continue
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    fields = ark.get("fields")
    if isinstance(fields, dict):
        # 先取语义明确的跳转字段，再对剩余字段做兜底扫描。
        for key in OFFICIAL_CARD_URL_KEYS:
            collect(fields.get(key))
        for key, value in fields.items():
            if key in OFFICIAL_CARD_URL_KEYS or key in OFFICIAL_CARD_NOISE_KEYS:
                continue
            collect(value)

    for key in ("prompt", "content", "desc", "title", "jump_url"):
        collect(ark.get(key))

    return candidates


def _scan_ark_values(node: Any) -> list[str]:
    """兜底扫描：跳过图标/封面类字段，收集卡片中的全部 URL。"""
    candidates: list[str] = []
    if isinstance(node, str):
        cleaned = _clean_embedded_url(node)
        candidates.extend(match.group(0) for match in URL_RE.finditer(cleaned))
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in OFFICIAL_CARD_NOISE_KEYS:
                continue
            candidates.extend(_scan_ark_values(value))
    elif isinstance(node, list):
        for item in node:
            candidates.extend(_scan_ark_values(item))
    return candidates


def extract_official_card_url(raw_message: Any) -> str | None:
    """从 QQ 官方机器人消息中提取小程序/分享卡片的跳转链接。

    QQ 官方接口不会把卡片转换成 Json 组件：卡片消息的正文会退化成
    「[卡片消息] 小程序 / 摘要: ...」这类纯文本，真正的跳转链接只保留在
    raw_message.ark_data（或引用消息 msg_elements[*].ark_data）中。
    这些摘要文本通常不含 URL，因此只看消息链会整条漏掉卡片。

    Args:
        raw_message: 平台原始消息对象（带 raw_data）或其负载 dict。

    Returns:
        卡片中的跳转链接，取不到时返回 None。
    """
    if isinstance(raw_message, (dict, list)):
        data: Any = raw_message
    else:
        data = getattr(raw_message, "raw_data", None)
    if not isinstance(data, (dict, list)):
        return None

    candidates: list[str] = []
    ark_nodes = list(_iter_official_ark(data))
    for ark in ark_nodes:
        candidates.extend(_official_ark_candidates(ark))
    if not candidates:
        # 字段名不固定时，退化为遍历整个卡片节点，跳过图标/封面类字段。
        for ark in ark_nodes:
            candidates.extend(_scan_ark_values(ark))

    return _pick_preferred_url(candidates)
