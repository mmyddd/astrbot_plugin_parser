from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def utils_module(monkeypatch: pytest.MonkeyPatch):
    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    astrbot_pkg = types.ModuleType("astrbot")
    astrbot_pkg.__path__ = []
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    monkeypatch.setitem(sys.modules, "astrbot", astrbot_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    monkeypatch.delitem(sys.modules, "core.utils", raising=False)
    return importlib.import_module("core.utils")


def test_extract_json_url_keeps_existing_detail_qqdocurl_support(utils_module):
    data = {"meta": {"detail_1": {"qqdocurl": "https://example.com/doc"}}}
    assert utils_module.extract_json_url(data) == "https://example.com/doc"


def test_extract_json_url_reads_qzone_miniapp_legacy_url(utils_module):
    data = {
        "meta": {
            "miniapp": {
                "legacyUrl": "https%3A%2F%2Fh5.qzone.qq.com%2Fugc%2Fshare%2F%3Fsid%3Dabc"
            }
        }
    }
    assert utils_module.extract_json_url(data).startswith(
        "https://h5.qzone.qq.com/ugc/share/?sid=abc"
    )


def test_extract_json_url_prefers_nested_qzone_share_over_unrelated_url(utils_module):
    data = {
        "prompt": "https://example.com/landing",
        "meta": {
            "detail": {
                "nested": "open https:\\/\\/mobile.qzone.qq.com\\/l?g=123 now"
            }
        },
    }
    assert utils_module.extract_json_url(data) == "https://mobile.qzone.qq.com/l?g=123"

def test_extract_official_card_url_reads_miniapp_ark_fields(utils_module):
    raw_message = {
        "message_type": 3,
        "ark_data": {
            "ark_type": "miniapp",
            "ark_name": "小程序",
            "fields": {
                "title": "你知道的 咪从小就没有妈妈",
                "source": "哔哩哔哩",
                "source_logo": "https://open.gtimg.cn/open/app_icon/x.png",
                "preview": "https://pic.ugcimg.cn/25447b837668ab3f66212a271d38a9b4/jpg1",
                "jump_url": "https://www.bilibili.com/video/BV1xx411c7mD?share_source=qq"
            },
        },
    }
    assert utils_module.extract_official_card_url(raw_message) == (
        "https://www.bilibili.com/video/BV1xx411c7mD?share_source=qq"
    )


def test_extract_official_card_url_ignores_image_only_cards(utils_module):
    raw_message = {
        "ark_data": {
            "ark_type": "picture",
            "fields": {
                "preview": "https://pic.example.com/a.jpg",
                "source_logo": "https://pic.example.com/logo.png",
            },
        },
    }
    assert utils_module.extract_official_card_url(raw_message) is None


def test_extract_official_card_url_walks_quoted_msg_elements(utils_module):
    raw_message = {
        "message_type": 103,
        "content": " ",
        "msg_elements": [
            {
                "message_type": 3,
                "ark_data": {
                    "ark_name": "图文 H5",
                    "fields": {"desc": "https:\\/\\/b23.tv\\/abcd123"},
                },
            }
        ],
    }
    assert utils_module.extract_official_card_url(raw_message) == "https://b23.tv/abcd123"


def test_extract_official_card_url_falls_back_to_plain_bilibili_link(utils_module):
    raw_message = {"ark_data": {"fields": {"title": "https://www.bilibili.com/video/BV1yy411c7mE"}}}
    assert utils_module.extract_official_card_url(raw_message) == (
        "https://www.bilibili.com/video/BV1yy411c7mE"
    )


def test_extract_official_card_url_handles_object_with_raw_data(utils_module):
    raw_message = SimpleNamespace(
        raw_data={
            "ark_data": {
                "ark_name": "小程序",
                "fields": {"jump_url": "https://www.xiaohongshu.com/explore/abc123"},
            }
        }
    )
    assert utils_module.extract_official_card_url(raw_message) == (
        "https://www.xiaohongshu.com/explore/abc123"
    )


def test_extract_official_card_url_returns_none_without_raw_data(utils_module):
    assert utils_module.extract_official_card_url(SimpleNamespace()) is None
    assert utils_module.extract_official_card_url(None) is None
