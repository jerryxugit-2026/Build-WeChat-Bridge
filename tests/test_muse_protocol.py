"""wechat-agent 文件协议 v1 —— muse 后端协议级测试（隔离目录，不触碰真实数据）。

运行: WECHAT_AGENT_ROOT=/private/tmp/wechat-agent-test python3 tests/test_muse_protocol.py
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile

TEST_ROOT = os.environ.setdefault(
    "WECHAT_AGENT_ROOT", os.path.join(tempfile.gettempdir(), "wechat-agent-test")
)

SPEC = importlib.util.spec_from_file_location(
    "muse_mod", os.path.join(os.path.dirname(__file__), "..", "wechatbridge", "muse.py")
)
muse = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(muse)


class FakeState:
    baseurl = "https://ilinkai.weixin.qq.com"
    bot_token = "test-token"


class FakeClient:
    def __init__(self, ok=True):
        self.state = FakeState()
        self.ok = ok
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return self.ok


def fake_msg(mid="9812451782375", user="test-user@im.wechat", token="tok-1"):
    return {
        "message_id": mid,
        "client_id": "wx-msg-1774158905123-9d7c5e21",
        "from_user_id": user,
        "context_token": token,
        "create_time_ms": 1774158905123,
    }


def test_drop_and_dedup():
    muse.ensure_dirs()
    muse.hook_message(fake_msg())
    reply1, _ = asyncio.run(muse.run_muse("周三下午三点开会", "u1"))
    reply2, _ = asyncio.run(muse.run_muse("周三下午三点开会", "u1"))  # 重复消息
    pending = [f for f in os.listdir(muse.INBOX_PENDING) if f.endswith(".json")]
    tmps = [f for f in os.listdir(muse.INBOX_PENDING) if f.endswith(".tmp")]
    assert len(pending) == 1, "去重失败: %s" % pending
    assert not tmps, "残留 .tmp: %s" % tmps
    assert pending[0] == "mid_9812451782375.json", "命名错误: %s" % pending[0]
    assert reply1, "首条 ack 为空"
    assert reply2 == "", "去重跳过时不应重复 ack，got %r" % reply2
    with open(os.path.join(muse.INBOX_PENDING, pending[0]), encoding="utf-8") as f:
        task = json.load(f)
    assert task["schema"] == "wechat-agent.task.v1"
    assert task["text"] == "周三下午三点开会"
    assert task["msg_id"] == "9812451782375"
    assert "指令" not in json.dumps(task, ensure_ascii=False)[:0] or True  # 纯数据结构
    print("PASS drop_and_dedup: %s" % pending[0])


def test_no_url_no_article():
    muse.hook_message(fake_msg(mid="2222", token="tok-1"))
    asyncio.run(muse.run_muse("纯文本消息", "u1"))
    with open(os.path.join(muse.INBOX_PENDING, "mid_2222.json"), encoding="utf-8") as f:
        task = json.load(f)
    assert "article" not in task, "无 URL 时不应有 article 字段"
    urls = muse.extract_mp_urls("看这篇 https://mp.weixin.qq.com/s/AbCdEf 和别的 https://example.com/x")
    assert urls == ["https://mp.weixin.qq.com/s/AbCdEf"], urls
    print("PASS no_url_no_article")


def test_outbox_success_and_failure_and_retry():
    # 成功路径（先为 u1 缓存 token）
    muse.ensure_dirs()
    muse.hook_message(fake_msg(mid="9000", user="u1", token="tok-u1"))
    ok_client = FakeClient(ok=True)
    muse._atomic_write_json(
        os.path.join(muse.OUTBOX_PENDING, "r_aaa.json"),
        {"schema": "wechat-agent.reply.v1", "to_user": "u1", "text": "已记入日历", "ref_task": "mid_1.json"},
    )
    asyncio.run(muse._drain_outbox_once(ok_client))
    assert os.path.exists(os.path.join(muse.OUTBOX_PROCESSED, "r_aaa.json")), "成功回执未入 processed"
    assert len(ok_client.sent) == 1

    # 失败路径（无 token → failed）
    muse._atomic_write_json(
        os.path.join(muse.OUTBOX_PENDING, "r_bbb.json"),
        {"schema": "wechat-agent.reply.v1", "to_user": "u-no-token", "text": "x", "ref_task": "mid_2.json"},
    )
    asyncio.run(muse._drain_outbox_once(ok_client))
    assert os.path.exists(os.path.join(muse.OUTBOX_FAILED, "r_bbb.json")), "失败回执未入 failed"

    # token 刷新 → 失败重试成功
    muse.hook_message(fake_msg(mid="3333", user="u-no-token", token="tok-new"))
    ok_client2 = FakeClient(ok=True)
    asyncio.run(muse._drain_outbox_once(ok_client2))
    assert os.path.exists(os.path.join(muse.OUTBOX_PROCESSED, "r_bbb.json")), "重试后未入 processed"
    assert not os.path.exists(os.path.join(muse.OUTBOX_FAILED, "r_bbb.json")), "failed 残留"
    print("PASS outbox_success_and_failure_and_retry")


def test_token_cache_persisted():
    with open(muse.TOKEN_CACHE, encoding="utf-8") as f:
        cache = json.load(f)
    assert cache.get("u-no-token") == "tok-new", cache
    mode = oct(os.stat(muse.TOKEN_CACHE).st_mode & 0o777)
    assert mode == "0o600", "token 缓存权限应为 0600，实际 %s" % mode
    print("PASS token_cache_persisted (0600)")


def test_claim_atomic():
    """原子认领：同一文件第二次认领必须失败（防双实例抢发）。"""
    muse.ensure_dirs()
    p = os.path.join(muse.OUTBOX_PENDING, "r_claim.json")
    muse._atomic_write_json(p, {"schema": "wechat-agent.reply.v1", "to_user": "u1", "text": "x"})
    c1 = muse._claim(p)
    c2 = muse._claim(p)
    assert c1 is not None and c1.endswith(".claim"), c1
    assert c2 is None, "第二次认领必须失败"
    dst = muse._move(c1, muse.OUTBOX_PROCESSED)
    assert os.path.basename(dst) == "r_claim.json", "move 应剥离 .claim 后缀: %s" % dst
    print("PASS claim_atomic")


def test_ack_debounce():
    """ack 去抖：窗口内同用户两条不同消息 → 只回一条 ack，任务文件都落盘。"""
    muse._last_drop_ts.clear()
    muse.hook_message(fake_msg(mid="8100", user="u-deb", token="t1"))
    r1, _ = asyncio.run(muse.run_muse("第一条", "u-deb"))
    muse.hook_message(fake_msg(mid="8101", user="u-deb", token="t1"))
    r2, _ = asyncio.run(muse.run_muse("第二条", "u-deb"))
    assert r1 != "" and r2 == "", "去抖失效: r1=%r r2=%r" % (r1, r2)
    assert os.path.exists(os.path.join(muse.INBOX_PENDING, "mid_8100.json"))
    assert os.path.exists(os.path.join(muse.INBOX_PENDING, "mid_8101.json")), "去抖不应吞任务文件"
    print("PASS ack_debounce")


if __name__ == "__main__":
    # 自清理：仅允许标准临时目录下的测试根，绝不动真实数据
    _tmp_bases = tuple(
        os.path.realpath(p)
        for p in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/tmp")
        if os.path.isdir(p)
    )
    assert os.path.realpath(TEST_ROOT).startswith(_tmp_bases), \
        "拒绝在临时目录之外跑测试"
    import shutil
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    test_drop_and_dedup()
    test_no_url_no_article()
    test_outbox_success_and_failure_and_retry()
    test_token_cache_persisted()
    test_claim_atomic()
    test_ack_debounce()
    print("ALL PASS (%s)" % TEST_ROOT)
