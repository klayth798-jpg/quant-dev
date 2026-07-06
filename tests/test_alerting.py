from dataclasses import replace

import quantdev.alerting as alerting_module
from quantdev.alerting import AlertService


def test_alert_failsafe_never_raises(monkeypatch):
    """webhook 发送抛错时，告警绝不向调用方抛异常（不能拖垮交易链路）。"""
    monkeypatch.setattr(
        alerting_module,
        "settings",
        replace(alerting_module.settings, alert_webhook_url="http://127.0.0.1:1/x"),
    )
    service = AlertService()

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(alerting_module.urllib.request, "urlopen", boom)
    # 不应抛异常
    service.send("标题", "内容", severity="critical")


def test_alert_dedup_window(monkeypatch):
    """相同 dedup_key 在去重窗口内只发送一次。"""
    monkeypatch.setattr(
        alerting_module,
        "settings",
        replace(alerting_module.settings, alert_webhook_url="http://example/webhook"),
    )
    service = AlertService()
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req)

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        return _Resp()

    monkeypatch.setattr(alerting_module.urllib.request, "urlopen", fake_urlopen)
    service.send("重复", "x", dedup_key="k")
    service.send("重复", "x", dedup_key="k")
    assert len(calls) == 1  # 第二次被去重


def test_alert_no_webhook_is_silent(monkeypatch):
    """未配置 webhook 时静默降级（只记日志，不发送、不抛错）。"""
    monkeypatch.setattr(
        alerting_module,
        "settings",
        replace(alerting_module.settings, alert_webhook_url=""),
    )
    sent = []
    monkeypatch.setattr(
        alerting_module.urllib.request, "urlopen", lambda *a, **k: sent.append(1)
    )
    AlertService().send("标题", "内容")
    assert sent == []
