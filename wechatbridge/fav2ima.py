"""fav2ima — 微信收藏 → ima 知识库桥(2026-10-25 新增,同日 LLM 路由 + 多类型扩展).

监听微信收藏库增量(经 wxeasy favorites --json),按收藏类型分流入 ima:

  文章(type=文章)      → import_urls(公众号 URL,ima 服务端抓取)
  图片(type=图片)      → 下载 → COS 上传 → add_knowledge(media_type=9,上限30MB)
  视频(视频号收藏)     → 组 Markdown(标题+链接+简介)→ import_doc 建笔记
                          → add_knowledge(media_type=11) 入知识库
  其他 http 文件 URL    → 按扩展名走上传管线(pdf/word/ppt/excel/md/txt/
                          epub/xmind/音频,大小按官方上限)
  其余类型(文本等)     → 跳过并推进水位(不卡队列),日志留痕

分流决策(按优先级):
  1. 规则文件显式命中(rules[].match)→ 用户强制指定,LLM 不介入
  2. LLM 判定(默认开):本机 CLIProxy 的 MiniMax-M3.1-Flash 按标题+公众号名
     选文件夹;返回 root/无效/调用失败 → 落默认库根目录,绝不阻塞入库
  3. 都没有 → 默认库根目录
LLM 端点环境变量:FAV2IMA_LLM_BASEURL(默认 http://127.0.0.1:8317/v1)、
FAV2IMA_LLM_MODEL(默认 MiniMax-M3.1-Flash)、FAV2IMA_LLM_APIKEY(可选)。
置 FAV2IMA_LLM_BASEURL=off 可整体关掉 LLM 判定。

路由规则: <WECHAT_AGENT_ROOT>/state/fav2ima_rules.yaml(每轮热加载)
  default_kb: <默认知识库ID>            # 留空则用 FAV2IMA_DEFAULT_KB
  folder_hints: {文件夹名: 语义描述}     # LLM prompt 用,优先于内容采样
  rules:
    - match: "公众号名 或 __biz 子串"     # 强制指定层,不走 LLM
      kb: <知识库ID>
      folder: <folder_id,可选>

文件夹语义上下文:从 ima 拉每个文件夹的最新 3 条标题做样例(24h 缓存)。
水位线: <WECHAT_AGENT_ROOT>/state/fav2ima_watermark.json;首跑建基线不回灌,
FAV2IMA_BACKLOG=1 可回灌。凭证失效(200002)暂停 30 分钟,不丢数据。
MiniMax-M 系是推理模型:回复含 <think> 段,解析时必须剥离,max_tokens 给足。
COS 上传签名按官方 cos-upload.cjs 移植(q-sign-algorithm=sha1,签 host+content-length)。
"""

import asyncio
import hashlib
import hmac
import html as _html
import json
import logging
import os
import random
import re
import time
from typing import Optional
from urllib.parse import quote as _urlquote

import httpx
import yaml

logger = logging.getLogger("fav2ima")

_WXEASY = os.environ.get("FAV2IMA_WXEASY", "wxeasy")
_IMA_BASE = "https://ima.qq.com/openapi"
_AGENT_ROOT = os.environ.get("WECHAT_AGENT_ROOT", os.path.expanduser("~/.wechat-agent"))
_STATE_DIR = os.path.join(_AGENT_ROOT, "state")
_WATERMARK_PATH = os.path.join(_STATE_DIR, "fav2ima_watermark.json")
_RULES_PATH = os.path.join(_STATE_DIR, "fav2ima_rules.yaml")
_FOLDER_CTX_PATH = os.path.join(_STATE_DIR, "fav2ima_folder_context.json")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_BIZ_RE = re.compile(r"__biz=([A-Za-z0-9+=/]+)")
_TITLE_RES = [
    re.compile(r'<meta property="og:title" content="([^"]*)"'),
    re.compile(r'<h1[^>]*id="activity-name"[^>]*>\s*(.*?)\s*<', re.S),
]
_NICK_RES = [
    re.compile(r'id="js_name"\s*>\s*(.*?)\s*<', re.S),
    re.compile(r'class="profile_nickname">([^<]*)<'),
    re.compile(r'rich_media_meta_nickname[^>]*>\s*<a[^>]*>\s*(.*?)\s*<', re.S),
]
_VIDEO_URL_RE = re.compile(r"(weixin\.qq\.com/sph/|finder\.video|channels/wei)")

_LLM_BASE = os.environ.get("FAV2IMA_LLM_BASEURL", "http://127.0.0.1:8317/v1").rstrip("/")
_LLM_MODEL = os.environ.get("FAV2IMA_LLM_MODEL", "MiniMax-M3.1-Flash")
_LLM_KEY = os.environ.get("FAV2IMA_LLM_APIKEY", "")
_FOLDER_CTX_TTL = 86400.0
_AUTH_DEAD_SECONDS = 1800
_FAIL_STREAK_LIMIT = 5
_DOWNLOAD_CAP = 200 << 20  # 绝对上限;具体类型上限查 _EXT_META

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)

_MB = 1 << 20
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# ext → (media_type, content_type, max_bytes);按官方《ima知识库 API》大小限制
_EXT_META = {
    "pdf": (1, "application/pdf", 200 * _MB),
    "doc": (3, "application/msword", 200 * _MB),
    "docx": (3, _DOCX, 200 * _MB),
    "ppt": (4, "application/vnd.ms-powerpoint", 200 * _MB),
    "pptx": (4, _PPTX, 200 * _MB),
    "xls": (5, "application/vnd.ms-excel", 10 * _MB),
    "xlsx": (5, _XLSX, 10 * _MB),
    "csv": (5, "text/csv", 10 * _MB),
    "md": (7, "text/markdown", 10 * _MB),
    "txt": (13, "text/plain", 10 * _MB),
    "html": (20, "text/html", 10 * _MB),
    "epub": (21, "application/epub+zip", 50 * _MB),
    "xmind": (14, "application/x-xmind", 10 * _MB),
    "jpg": (9, "image/jpeg", 30 * _MB), "jpeg": (9, "image/jpeg", 30 * _MB),
    "png": (9, "image/png", 30 * _MB), "webp": (9, "image/webp", 30 * _MB),
    "mp3": (15, "audio/mpeg", 200 * _MB), "m4a": (15, "audio/x-m4a", 200 * _MB),
    "wav": (15, "audio/wav", 200 * _MB), "aac": (15, "audio/aac", 200 * _MB),
}
_EXT_BY_CTYPE = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "application/pdf": "pdf", "text/markdown": "md", "text/plain": "txt",
    "application/epub+zip": "epub", "application/x-xmind": "xmind",
    "audio/mpeg": "mp3", "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/aac": "aac",
    _DOCX: "docx", _XLSX: "xlsx", _PPTX: "pptx", "text/html": "html", "text/csv": "csv",
}


def enabled() -> bool:
    return all(
        os.environ.get(k, "").strip()
        for k in ("IMA_OPENAPI_CLIENTID", "IMA_OPENAPI_APIKEY", "FAV2IMA_DEFAULT_KB")
    )


def llm_enabled() -> bool:
    """LLM 路由开关;置 FAV2IMA_LLM_BASEURL=off 显式关闭。"""
    return bool(_LLM_BASE) and _LLM_BASE.lower() != "off" and bool(_LLM_MODEL)


def _atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_watermark() -> Optional[int]:
    try:
        with open(_WATERMARK_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("watermark", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_watermark(mid: int) -> None:
    _atomic_write_json(_WATERMARK_PATH, {"watermark": mid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})


def load_rules() -> dict:
    """读路由配置;文件缺失/非法时回退到环境变量默认库。"""
    default_kb = os.environ.get("FAV2IMA_DEFAULT_KB", "").strip()
    rules: list = []
    hints: dict = {}
    try:
        with open(_RULES_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            if str(data.get("default_kb", "") or "").strip():
                default_kb = str(data["default_kb"]).strip()
            raw = data.get("rules")
            if isinstance(raw, list):
                rules = [r for r in raw if isinstance(r, dict) and r.get("match") and r.get("kb")]
            raw_h = data.get("folder_hints")
            if isinstance(raw_h, dict):
                hints = {str(k): str(v) for k, v in raw_h.items() if v}
    except OSError:
        pass
    except yaml.YAMLError as e:
        logger.warning("路由规则文件解析失败,全部走默认库: %s", e)
    return {"default_kb": default_kb, "rules": rules, "hints": hints}


def route(rules: dict, url: str, nickname: str) -> tuple[str, Optional[str], bool]:
    """规则显式命中 → (kb, folder|None, True);未命中 → (default_kb, None, False)。"""
    for r in rules.get("rules", []):
        m = str(r.get("match", ""))
        if m and (m in (nickname or "") or m in (url or "")):
            kb = str(r.get("kb", "")).strip() or rules["default_kb"]
            folder = str(r.get("folder", "") or "").strip() or None
            return kb, folder, True
    return rules["default_kb"], None, False


def pick_new(items: list, watermark: int) -> list:
    """水位之上的全部新收藏(含图片/视频/文件),按 id 升序;类型分发在处理层。"""
    out = [
        i for i in items
        if isinstance(i, dict)
        and isinstance(i.get("id"), int)
        and i["id"] > watermark
    ]
    return sorted(out, key=lambda x: x["id"])


def parse_title(page_html: str) -> tuple[str, str]:
    """从公众号文章页解析 (标题, 公众号名);解析不到给空串。"""
    text = _html.unescape(page_html or "")
    title, nick = "", ""
    for pat in _TITLE_RES:
        m = pat.search(text)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if title:
                break
    for pat in _NICK_RES:
        m = pat.search(text)
        if m:
            nick = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if nick:
                break
    return title, nick


def extract_folder(reply: str, valid_names: set) -> Optional[str]:
    """从 LLM 回复剥 <think>/围栏后提取文件夹名;无效返回 None。"""
    txt = _THINK_RE.sub("", reply or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    folder = str(obj.get("folder", "") or "").strip()
    if folder == "root":
        return None
    return folder if folder in valid_names else None


def ext_from_url(url: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,8})(?:[?#]|$)", url or "")
    return m.group(1).lower() if m else ""


def compose_video_note(it: dict) -> tuple[str, str]:
    """视频号收藏 → (标题, Markdown 正文)。
    title/des/url 来自 wxeasy 0.4.1 补丁版(favitem XML 的 title/des/link|url 标签)。"""
    title = (str(it.get("title", "") or "").strip()
             or f"视频号内容_{it.get('id', '')}")
    desc = (str(it.get("des", "") or "").strip()
            or (str(it.get("from", "")) or "微信收藏"))
    url = str(it.get("url", "") or "")
    body = f"# {title}\n\n- 链接: {url}\n- 来源: 微信收藏 · 视频号 · {it.get('time', '')}\n\n{desc}\n"
    return title, body


async def fetch_article_meta(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """抓文章页取标题/公众号名,仅用于路由与日志;失败不阻塞导入。"""
    try:
        r = await client.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=10)
        if r.status_code == 200:
            return parse_title(r.text)
    except Exception as e:
        logger.debug("抓取文章页失败(%s): %s", url[:80], e)
    return "", ""


async def list_new_favorites() -> list:
    proc = await asyncio.create_subprocess_exec(
        _WXEASY, "favorites", "-n", "30", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"wxeasy favorites 退出码 {proc.returncode}: {err.decode()[:200]}")
    data = json.loads(out.decode() or "{}")
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("favorites") or []


async def ima_post(client: httpx.AsyncClient, path: str, body: dict) -> tuple[int, str, dict]:
    headers = {
        "ima-openapi-clientid": os.environ["IMA_OPENAPI_CLIENTID"].strip(),
        "ima-openapi-apikey": os.environ["IMA_OPENAPI_APIKEY"].strip(),
        "ima-openapi-ctx": "wechatbridge-fav2ima/1.0",
    }
    r = await client.post(f"{_IMA_BASE}/{path}", json=body, headers=headers, timeout=30)
    j = r.json()
    return int(j.get("code", -1)), str(j.get("msg", "")), j.get("data") or {}


def _items_of(data: dict) -> list:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("knowledge_list"), list):
        return data["knowledge_list"]
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


async def folder_context(client: httpx.AsyncClient, kb_id: str) -> dict:
    """{文件夹名: {"id":…, "titles":[≤3条最新标题]}};24h 缓存。"""
    try:
        with open(_FOLDER_CTX_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("kb") == kb_id and time.time() - cache.get("ts", 0) < _FOLDER_CTX_TTL:
            return cache.get("folders", {})
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    ctx: dict = {}
    code, msg, data = await ima_post(client, "wiki/v1/get_knowledge_list", {"knowledge_base_id": kb_id, "limit": 50})
    if code != 0:
        logger.warning("拉取文件夹列表失败 code=%s msg=%s", code, msg)
        return {}
    for it in _items_of(data):
        if it.get("media_type") == 99 and it.get("media_id") and it.get("title"):
            ctx[str(it["title"])] = {"id": it["media_id"], "titles": []}
    for name, info in ctx.items():
        _, _, d2 = await ima_post(client, "wiki/v1/get_knowledge_list", {
            "knowledge_base_id": kb_id, "folder_id": info["id"], "limit": 3})
        info["titles"] = [str(i.get("title", ""))[:40] for i in _items_of(d2)
                          if isinstance(i, dict) and i.get("title")][:3]
    _atomic_write_json(_FOLDER_CTX_PATH, {"kb": kb_id, "ts": time.time(), "folders": ctx})
    logger.info("fav2ima 文件夹上下文已刷新: %d 个文件夹", len(ctx))
    return ctx


async def llm_classify(client: httpx.AsyncClient, ctx: dict, hints: dict,
                       title: str, nickname: str) -> Optional[str]:
    """LLM 判定文件夹名;root/失败/无效 → None。"""
    lines = []
    for name, info in ctx.items():
        hint = hints.get(name)
        if hint:
            desc = hint
        elif info.get("titles"):
            desc = "样例: " + " / ".join(info["titles"][:3])
        else:
            desc = ""
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    sys_prompt = (
        "你是个人知识库的路由器。把文章分入最合适的文件夹;所有文件夹都不合适时返回 root。\n"
        "文件夹清单(名称: 语义或内容样例):\n" + "\n".join(lines) +
        '\n只输出一行 JSON: {"folder":"文件夹名或root","reason":"不超过12字"} ,不要输出其他内容。'
    )
    user = f"文章标题:「{title}」" + (f" 公众号:{nickname}" if nickname else "")
    headers = {"Authorization": f"Bearer {_LLM_KEY}"} if _LLM_KEY else {}
    r = await client.post(
        f"{_LLM_BASE}/chat/completions",
        json={"model": _LLM_MODEL, "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ], "temperature": 0, "max_tokens": 4096},
        headers=headers, timeout=90,
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"]
    return extract_folder(reply, set(ctx.keys()))


async def resolve_folder(http: httpx.AsyncClient, rules: dict, kb: str, url: str,
                         title: str, nick: str, it: dict) -> tuple[str, Optional[str]]:
    """规则 → LLM → 默认;返回 (kb, folder|None)。"""
    kb2, folder, matched = route(rules, url, nick)
    if matched:
        return kb2, folder
    if llm_enabled():
        try:
            ctx = await folder_context(http, kb2)
            if ctx:
                name = await llm_classify(http, ctx, rules.get("hints", {}), title or url, nick)
                return kb2, (ctx.get(name, {}).get("id") if name else None)
        except Exception as e:
            logger.warning("fav2ima id=%s LLM 判定失败(%s),落默认库根目录", it.get("id"), e)
    return kb2, None


def cos_upload_sync(cred: dict, file_path: str, content_type: str) -> bool:
    """腾讯云 COS PUT Object(签名按官方 cos-upload.cjs 移植)。"""
    secret_id, secret_key, token = cred["secret_id"], cred["secret_key"], cred["token"]
    bucket, region, cos_key = cred["bucket_name"], cred["region"], cred["cos_key"]
    start = str(int(cred.get("start_time") or time.time()))
    expired = str(int(cred.get("expired_time") or (time.time() + 3600)))
    key_time = f"{start};{expired}"
    host = f"{bucket}.cos.{region}.myqcloud.com"
    pathname = f"/{cos_key}"
    size = str(os.path.getsize(file_path))
    sign_headers = {"content-length": size, "host": host}
    keys = sorted(sign_headers)
    http_headers = "&".join(f"{k}={_urlquote(sign_headers[k], safe='')}" for k in keys)
    http_string = f"put\n{pathname}\n\n{http_headers}\n"
    sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
    auth = "&".join([
        "q-sign-algorithm=sha1", f"q-ak={secret_id}", f"q-sign-time={key_time}",
        f"q-key-time={key_time}", "q-header-list=" + ";".join(keys), "q-url-param-list=",
        f"q-signature={signature}",
    ])
    with open(file_path, "rb") as fh:
        data = fh.read()
    r = httpx.put(f"https://{host}{pathname}", content=data, headers={
        "Content-Type": content_type, "Content-Length": size,
        "Authorization": auth, "x-cos-security-token": token,
    }, timeout=600)
    return 200 <= r.status_code < 300


async def download_to(http: httpx.AsyncClient, url: str, dest: str, max_bytes: int) -> Optional[str]:
    """流式下载到 dest;返回实际保存路径,超限/失败返回 None。"""
    tmp = dest + ".part"
    total = 0
    try:
        async with httpx.stream("GET", url, headers={"User-Agent": _UA},
                                follow_redirects=True, timeout=120) as r:
            if r.status_code != 200:
                logger.debug("下载 HTTP %s: %s", r.status_code, url[:80])
                return None
            with open(tmp, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    total += len(chunk)
                    if total > max_bytes:
                        os.unlink(tmp)
                        logger.warning("下载超限(>%d bytes): %s", max_bytes, url[:80])
                        return None
                    f.write(chunk)
    except Exception as e:
        logger.debug("下载失败(%s): %s", url[:80], e)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    os.replace(tmp, dest)
    return dest


async def upload_via_pipeline(http: httpx.AsyncClient, local: str, *, kb: str, folder: Optional[str],
                              title: str, media_type: int, content_type: str, ext: str) -> tuple[int, str]:
    """create_media → COS → add_knowledge。返回 (code, msg)。"""
    size = os.path.getsize(local)
    code, msg, data = await ima_post(http, "wiki/v1/create_media", {
        "file_name": title, "file_size": size, "content_type": content_type,
        "knowledge_base_id": kb, "file_ext": ext})
    if code != 0:
        return code, f"create_media: {msg}"
    cred = data.get("cos_credential") or {}
    media_id = data.get("media_id") or ""
    ok = await asyncio.to_thread(cos_upload_sync, cred, local, content_type)
    if not ok:
        return -2, "COS 上传失败"
    body: dict = {"media_type": media_type, "media_id": media_id, "title": title,
                  "knowledge_base_id": kb, "file_info": {
                      "cos_key": cred.get("cos_key", ""), "file_size": size,
                      "last_modify_time": int(time.time()), "file_name": title}}
    if folder:
        body["folder_id"] = folder
    return await ima_post(http, "wiki/v1/add_knowledge", body)


async def handle_one(http: httpx.AsyncClient, it: dict, rules: dict) -> tuple[str, int, str]:
    """单条收藏 → ima。返回 (状态, code, msg);状态 ∈ ok/auth/fail/skip。"""
    mid = it.get("id")
    ttype = str(it.get("type", ""))
    url = str(it.get("url", "") or "")
    if ttype == "文章" and url.startswith("http"):
        title, nick = await fetch_article_meta(http, url)
        if not title and "pages/image_detail" in url:
            # 新版公众号图片帖:页面为 JS 渲染壳,标题/公众号名均不可得(2026-10-26 实证,
            # 本地 biz 缓存亦常缺)→ 零信号,不走 LLM,确定性落「生活」兜底夹
            title = f"公众号图片帖_{mid}"
            kb = rules["default_kb"]
            try:
                ctx = await folder_context(http, kb)
                fallback = os.environ.get("FAV2IMA_FALLBACK_FOLDER", "生活")
                folder = (ctx.get(fallback) or {}).get("id")
            except Exception:
                folder = None
            logger.info("fav2ima id=%s 图片帖无标题,直落兜底夹 (folder=%s)", mid, bool(folder))
        else:
            kb, folder = await resolve_folder(http, rules, rules["default_kb"], url, title, nick, it)
        body: dict = {"knowledge_base_id": kb, "urls": [url]}
        if folder:
            body["folder_id"] = folder
        code, msg, _ = await ima_post(http, "wiki/v1/import_urls", body)
        if code == 0:
            logger.info("已入ima(文章) id=%s 「%s」(%s) → kb=%s%s", mid, title or "(无题)",
                        nick or "?", kb, f"/{folder}" if folder else "")
            return "ok", code, msg
        return ("auth" if code == 200002 else "fail"), code, msg

    if ttype == "图片" or (ttype not in ("视频",) and url.startswith("http") and ext_from_url(url) in _EXT_META):
        if not url.startswith("http"):
            # 图片/文件收藏的 XML 通常只有 datasourceid(需鉴权 CDN,无公开 URL),
            # 2026-10-25 实证:wxeasy 补丁后仍无 url 字段 → 干净跳过,不进重试。
            return "skip", 0, "图片/文件收藏无可下载 URL(微信收藏 CDN 需鉴权)"
        ext = ext_from_url(url) or "jpg"
        meta = _EXT_META.get(ext)
        if meta is None:
            return "skip", 0, f"未知扩展名 {ext}"
        mtype, ctype, cap = meta
        title = f"收藏图片_{mid}" if ttype == "图片" else f"收藏文件_{mid}.{ext}"
        local = await download_to(http, url, os.path.join(_STATE_DIR, f"fav_{mid}.{ext}"), cap)
        if local is None:
            return "fail", -1, "下载失败或超限"
        try:
            kb, folder = await resolve_folder(http, rules, rules["default_kb"], url, title, "", it)
            code, msg = await upload_via_pipeline(http, local, kb=kb, folder=folder, title=title,
                                                  media_type=mtype, content_type=ctype, ext=ext)
            if code == 0:
                logger.info("已入ima(%s) id=%s 「%s」→ kb=%s%s", ttype, mid, title,
                            kb, f"/{folder}" if folder else "")
                return "ok", code, msg
            return ("auth" if code == 200002 else "fail"), code, msg
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass

    if ttype == "视频" or _VIDEO_URL_RE.search(url):
        if not url:
            return "skip", 0, "视频号收藏无链接(XML 里无 link/url 标签)"
        title, body_md = compose_video_note(it)
        kb, folder = await resolve_folder(http, rules, rules["default_kb"], url, title, "", it)
        code, msg, data = await ima_post(http, "note/v1/import_doc", {
            "content_format": 1, "content": body_md})
        if code != 0:
            return ("auth" if code == 200002 else "fail"), code, f"import_doc: {msg}"
        note_id = (data or {}).get("note_id", "")
        body: dict = {"media_type": 11, "title": title, "knowledge_base_id": kb,
                      "note_info": {"content_id": note_id}}
        if folder:
            body["folder_id"] = folder
        code, msg, _ = await ima_post(http, "wiki/v1/add_knowledge", body)
        if code == 0:
            logger.info("已入ima(视频号笔记) id=%s 「%s」→ kb=%s%s", mid, title,
                        kb, f"/{folder}" if folder else "")
            return "ok", code, msg
        return ("auth" if code == 200002 else "fail"), code, msg

    return "skip", 0, f"类型 {ttype or '?' } 不处理"


async def fav2ima_loop() -> None:
    if not enabled():
        logger.info("fav2ima 未启用(缺 IMA_OPENAPI_CLIENTID/APIKEY 或 FAV2IMA_DEFAULT_KB),退出")
        return
    poll = float(os.environ.get("FAV2IMA_POLL_SECONDS", "120"))
    os.makedirs(_STATE_DIR, exist_ok=True)
    logger.info("fav2ima 启动: 轮询 %ss, 默认库=%s, LLM=%s(%s)",
                poll, os.environ["FAV2IMA_DEFAULT_KB"].strip(),
                _LLM_MODEL if llm_enabled() else "off", _LLM_BASE if llm_enabled() else "-")

    async with httpx.AsyncClient() as http:
        wm = load_watermark()
        if wm is None:
            items = await list_new_favorites()
            wm = max((i["id"] for i in items if isinstance(i, dict) and isinstance(i.get("id"), int)), default=0)
            if os.environ.get("FAV2IMA_BACKLOG", "").strip() == "1":
                wm = 0
            save_watermark(wm)
            logger.info("fav2ima 基线水位 id=%s (backlog=%s)", wm, os.environ.get("FAV2IMA_BACKLOG") == "1")

        fail_streak = 0
        while True:
            try:
                rules = load_rules()
                items = await list_new_favorites()
                for it in pick_new(items, wm):
                    status, code, msg = await handle_one(http, it, rules)
                    mid = it.get("id", 0)
                    if status == "ok":
                        wm = mid
                        save_watermark(mid)
                        fail_streak = 0
                    elif status == "auth":
                        logger.error("ima 凭证失效(200002):暂停导入 %s 分钟,请更新 Client ID/API Key",
                                     _AUTH_DEAD_SECONDS // 60)
                        await asyncio.sleep(_AUTH_DEAD_SECONDS)
                        break
                    elif status == "skip":
                        wm = mid
                        save_watermark(mid)
                        logger.info("fav2ima id=%s 跳过(%s),水位推进", mid, msg)
                    else:
                        fail_streak += 1
                        logger.warning("处理失败 id=%s code=%s msg=%s (连续 %s)", mid, code, msg, fail_streak)
                        if fail_streak >= _FAIL_STREAK_LIMIT:
                            wm = mid
                            save_watermark(mid)
                            fail_streak = 0
                            logger.error("id=%s 连续失败 %s 次,放弃并推进水位", mid, _FAIL_STREAK_LIMIT)
                        else:
                            await asyncio.sleep(10)
            except Exception as e:
                logger.exception("fav2ima 轮询异常: %s", e)
            await asyncio.sleep(poll + random.uniform(0, poll * 0.2))


# ──────────────────────────────────────────────────────────────
# 视频号分享截胡(2026-10-25):转发给 Muse Bot 的视频号卡片 → ima 笔记
# main.py 在 muse hook 之前调用 import_sph_share;muse 零感知。
# ──────────────────────────────────────────────────────────────

_SPH_URL_RE = re.compile(r"https?://[\w.-]*weixin\.qq\.com/sph/[A-Za-z0-9_-]+")


def _extract_sph_url(text: str) -> str:
    """从消息文本/卡片链接中提取视频号 URL;无则空串。"""
    m = _SPH_URL_RE.search(text or "")
    return m.group(0) if m else ""


async def import_sph_share(client, from_user: str, context_token: str,
                           url: str, raw_text: str) -> None:
    """视频号分享 → import_doc 建笔记 → add_knowledge 入库;回执发微信 + 推 HUD。"""
    rules = load_rules()
    lines = [l.strip() for l in (raw_text or "").splitlines()
             if l.strip() and not l.strip().startswith("http")]
    title = lines[0][:60] if lines else f"视频号分享_{int(time.time())}"
    desc = " ".join(lines[1:3])[:120] if len(lines) > 1 else ""
    body_md = (
        f"# {title}\n\n"
        f"- 链接: {url}\n"
        f"- 来源: 微信视频号分享 · {time.strftime('%Y-%m-%d %H:%M')}\n\n"
        + (desc + "\n" if desc else "")
    )
    async with httpx.AsyncClient() as http:
        code, msg, data = await ima_post(http, "note/v1/import_doc",
                                         {"content_format": 1, "content": body_md})
        if code != 0:
            logger.error("视频号分享建笔记失败 code=%s msg=%s", code, msg)
            await _sph_reply(client, from_user, context_token, f"⚠️ 视频号入 ima 失败: {msg}")
            return
        note_id = (data or {}).get("note_id", "")
        kb, folder = await resolve_folder(http, rules, rules["default_kb"],
                                          url, title, "", {"id": 0})
        add: dict = {"media_type": 11, "title": title, "knowledge_base_id": kb,
                     "note_info": {"content_id": note_id}}
        if folder:
            add["folder_id"] = folder
        code, msg, _ = await ima_post(http, "wiki/v1/add_knowledge", add)
    if code == 0:
        logger.info("已入ima(视频号分享) 「%s」→ kb=%s%s", title, kb, f"/{folder}" if folder else "")
        await _sph_reply(client, from_user, context_token, f"✅ 已入 ima:「{title}」")
        token = os.environ.get("HUD_PUSH_TOKEN", "").strip()
        if token:
            asyncio.create_task(_hud_push_http(token, title))
    else:
        logger.error("视频号分享 add_knowledge 失败 code=%s msg=%s", code, msg)
        await _sph_reply(client, from_user, context_token, f"⚠️ 视频号入 ima 失败: {msg}")


async def _sph_reply(client, from_user: str, context_token: str, text: str) -> None:
    try:
        await client.send_message(to_user_id=from_user, text=text,
                                  context_token=context_token,
                                  baseurl=client.state.baseurl,
                                  bot_token=client.state.bot_token)
    except Exception as e:
        logger.warning("视频号分享回执发送失败: %s", e)


async def _hud_push_http(token: str, title: str) -> None:
    """推一条 HUD 通知(经 worker /hud/push);失败静默。"""
    try:
        url = os.environ.get("HUD_PUSH_URL", "").strip()
        if not url:
            return
        async with httpx.AsyncClient() as http:
            await http.post(
                url,
                headers={"authorization": f"Bearer {token}",
                         "user-agent": "Mozilla/5.0 fav2ima/1.0"},
                json={"app": "fav2ima", "title": f"已入ima:{title}"[:60],
                      "subtitle": "微信视频号分享", "body": title[:200]},
                timeout=15)
    except Exception as e:
        logger.debug("HUD push 失败: %s", e)
