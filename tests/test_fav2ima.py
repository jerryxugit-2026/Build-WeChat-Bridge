"""fav2ima 纯函数测试:路由、水位筛选、标题解析、规则加载、LLM 回复解析、多类型分发辅助。"""

import os

import pytest

from wechatbridge import fav2ima


def test_route_hits_nickname_rule():
    rules = {"default_kb": "kb_default", "rules": [{"match": "老石谈芯", "kb": "kb_chip"}]}
    assert fav2ima.route(rules, "https://mp.weixin.qq.com/s?__biz=MzA=", "老石谈芯") == ("kb_chip", None, True)


def test_route_hits_biz_rule_with_folder():
    rules = {"default_kb": "kb_default", "rules": [{"match": "MzUyNTc2", "kb": "kb_x", "folder": "folder_y"}]}
    kb, folder, matched = fav2ima.route(rules, "https://mp.weixin.qq.com/s?__biz=MzUyNTc2OTU0Ng==&mid=1", "")
    assert (kb, folder, matched) == ("kb_x", "folder_y", True)


def test_route_falls_back_to_default_unmatched():
    rules = {"default_kb": "kb_default", "rules": [{"match": "不存在", "kb": "kb_x"}]}
    assert fav2ima.route(rules, "https://mp.weixin.qq.com/s?__biz=Other", "别的号") == ("kb_default", None, False)


def test_pick_new_includes_all_types_above_watermark_sorted():
    items = [
        {"id": 3, "type": "文章", "url": "https://a"},
        {"id": 1, "type": "图片", "url": "https://b/x.png"},
        {"id": 2, "type": "视频", "url": "https://sph/x"},
        {"id": 0, "type": "文章", "url": "https://e"},   # 水位之下跳过
        "garbage",                                        # 非法条目跳过
        {"id": 4, "type": "文本", "url": ""},             # 新语义:全部放行,分发层跳过
    ]
    got = fav2ima.pick_new(items, watermark=0)
    assert [i["id"] for i in got] == [1, 2, 3, 4]


def test_pick_new_respects_watermark():
    items = [{"id": 9, "type": "文章", "url": "https://a"}, {"id": 10, "type": "文章", "url": "https://b"}]
    assert [i["id"] for i in fav2ima.pick_new(items, 9)] == [10]


def test_ext_from_url():
    assert fav2ima.ext_from_url("https://x/a/b.PNG?sign=1") == "png"
    assert fav2ima.ext_from_url("https://x/a/b.docx") == "docx"
    assert fav2ima.ext_from_url("https://x/a/noext") == ""


def test_ext_meta_has_common_types():
    for ext in ("pdf", "docx", "md", "png", "jpg", "mp3", "epub"):
        mtype, ctype, cap = fav2ima._EXT_META[ext]
        assert mtype > 0 and ctype and cap >= 10 << 20


def test_compose_video_note_with_fields():
    it = {"id": 77, "time": "2026-10-25 20:00", "from": "张三",
          "url": "https://channels.weixin.qq.com/sph/abc",
          "title": "某个视频标题", "des": "视频简介文字"}
    title, body = fav2ima.compose_video_note(it)
    assert title == "某个视频标题"
    assert "channels.weixin.qq.com/sph/abc" in body
    assert "视频简介文字" in body


def test_compose_video_note_fallback_title():
    it = {"id": 88, "url": "https://weixin.qq.com/sph/xyz", "preview": ""}
    title, body = fav2ima.compose_video_note(it)
    assert "88" in title
    assert "https://weixin.qq.com/sph/xyz" in body


def test_parse_title_og_and_js_name():
    page = '<html><meta property="og:title" content="深度学习入门"><div id="js_name">机器之心</div></html>'
    assert fav2ima.parse_title(page) == ("深度学习入门", "机器之心")


def test_parse_title_garbage_returns_empty():
    assert fav2ima.parse_title("<html>nothing</html>") == ("", "")


def test_load_rules_missing_file_uses_env_default(tmp_path, monkeypatch):
    monkeypatch.setattr(fav2ima, "_RULES_PATH", str(tmp_path / "nope.yaml"))
    monkeypatch.setenv("FAV2IMA_DEFAULT_KB", "kb_env")
    rules = fav2ima.load_rules()
    assert rules == {"default_kb": "kb_env", "rules": [], "hints": {}}


def test_load_rules_valid_file_overrides(tmp_path, monkeypatch):
    p = tmp_path / "rules.yaml"
    p.write_text(
        "default_kb: kb_file\n"
        "folder_hints:\n"
        "  AI: 技术大杂烩\n"
        "rules:\n"
        "  - match: 老石谈芯\n"
        "    kb: kb_chip\n"
        "    folder: folder_1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fav2ima, "_RULES_PATH", str(p))
    monkeypatch.setenv("FAV2IMA_DEFAULT_KB", "kb_env")
    rules = fav2ima.load_rules()
    assert rules["default_kb"] == "kb_file"
    assert rules["hints"] == {"AI": "技术大杂烩"}
    assert fav2ima.route(rules, "https://x", "老石谈芯") == ("kb_chip", "folder_1", True)


def test_extract_folder_plain_json():
    valid = {"AI", "生活", "科研"}
    assert fav2ima.extract_folder('{"folder":"AI","reason":"工具"}', valid) == "AI"


def test_extract_folder_think_wrapped():
    reply = '<think>需要判断...</think>\n{"folder":"科研","reason":"论文工具"}'
    assert fav2ima.extract_folder(reply, {"科研"}) == "科研"


def test_extract_folder_root_returns_none():
    assert fav2ima.extract_folder('{"folder":"root","reason":"无"}', {"AI"}) is None


def test_extract_folder_invalid_name_returns_none():
    assert fav2ima.extract_folder('{"folder":"不存在的夹","reason":"x"}', {"AI"}) is None


def test_llm_enabled_toggle(monkeypatch):
    monkeypatch.setattr(fav2ima, "_LLM_BASE", "http://127.0.0.1:8317/v1")
    monkeypatch.setattr(fav2ima, "_LLM_MODEL", "MiniMax-M3.1-Flash")
    assert fav2ima.llm_enabled() is True
    monkeypatch.setattr(fav2ima, "_LLM_BASE", "off")
    assert fav2ima.llm_enabled() is False


def test_enabled_requires_all_three(monkeypatch):
    for k in ("IMA_OPENAPI_CLIENTID", "IMA_OPENAPI_APIKEY", "FAV2IMA_DEFAULT_KB"):
        monkeypatch.delenv(k, raising=False)
    assert fav2ima.enabled() is False
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "cid")
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "key")
    assert fav2ima.enabled() is False
    monkeypatch.setenv("FAV2IMA_DEFAULT_KB", "kb")
    assert fav2ima.enabled() is True


def test_extract_sph_url_hits_channels_link():
    assert fav2ima._extract_sph_url(
        "某个视频标题\nhttps://channels.weixin.qq.com/sph/ABCdef123") == \
        "https://channels.weixin.qq.com/sph/ABCdef123"


def test_extract_sph_url_ignores_mp_article():
    assert fav2ima._extract_sph_url(
        "文章\nhttps://mp.weixin.qq.com/s?__biz=MzA=") == ""


def test_image_detail_marker_in_url():
    """图片帖判定:无标题+image_detail URL → 走生活兜底分支(代码内联,这里验条件本身)。"""
    assert "pages/image_detail" in "https://mp.weixin.qq.com/s?t=pages/image_detail&scene=24&__biz=X"
    assert bool("pages/image_detail" in "https://mp.weixin.qq.com/s?__biz=X&mid=1") is False
