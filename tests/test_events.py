"""events：事件构造 / 总线 / 取消令牌。"""

import queue
import threading

from wmts.core.events import (
    EV_DONE,
    EV_LOG,
    EV_PROGRESS,
    CancelToken,
    EventBus,
    make_event,
)


def test_make_event_fills_schema():
    ev = make_event(EV_PROGRESS, stage="download", done=1, total=2,
                    percent=50.0, counters={"success": 1})
    assert ev["type"] == "progress"
    assert ev["stage"] == "download"
    assert ev["done"] == 1 and ev["total"] == 2
    assert ev["percent"] == 50.0
    assert ev["counters"] == {"success": 1}
    assert ev["tile"] is None and ev["message"] is None
    assert ev["ts"] > 0


def test_make_event_ignores_unknown_fields():
    ev = make_event(EV_DONE, bogus="x")
    assert "bogus" not in ev
    assert "result" in ev  # result 是合法字段


def test_eventbus_pubsub():
    bus = EventBus()
    sid, q = bus.subscribe()
    bus.publish({"type": EV_LOG, "message": "a"})
    assert q.get(timeout=1)["message"] == "a"
    bus.unsubscribe(sid)
    bus.publish({"type": EV_LOG, "message": "b"})  # 已退订，不抛错


def test_eventbus_log_history():
    bus = EventBus(log_buffer_size=5)
    for i in range(8):
        bus.publish({"type": EV_LOG, "message": str(i)})
    hist = bus.log_history()
    assert len(hist) == 5
    assert hist[-1]["message"] == "7"
    assert bus.log_history(limit=2)[-1]["message"] == "7"


def test_eventbus_slow_subscriber_drops():
    bus = EventBus(queue_size=1)
    sid, q = bus.subscribe()
    for i in range(5):
        bus.publish({"type": EV_LOG, "message": str(i)})  # 不应阻塞/抛错
    assert q.qsize() <= 1


def test_eventbus_thread_safety():
    bus = EventBus()
    sid, q = bus.subscribe()
    def worker():
        for i in range(100):
            bus.publish({"type": EV_LOG, "message": str(i)})
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q.qsize() == 400
    bus.unsubscribe(sid)


def test_cancel_token():
    token = CancelToken()
    assert not token.cancelled
    assert not token.is_set()
    token.cancel("测试")
    assert token.cancelled and token.is_set()
    assert token.reason == "测试"
    token.cancel("再次")  # 幂等，保留首个原因
    assert token.reason == "测试"
