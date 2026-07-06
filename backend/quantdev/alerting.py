"""企业微信告警通道：把关键实盘事件主动推送给运维，避免故障静默。

实盘最危险的不是出错，而是“出错了没人知道”。本模块把 Kill Switch 激活、对账
BREAK、订单进入 UNKNOWN、运行器循环失败等事件主动推送到企业微信群机器人。

设计：
  - 通过企业微信群机器人 webhook 发送 markdown 消息（用标准库 urllib，无新增依赖）。
  - fail-safe：发送失败只记日志，绝不向调用方抛异常——告警不能拖垮交易链路。
  - 去重：相同 dedup_key 在 dedup_window 秒内只发一次，避免告警风暴。
  - 重试：发送失败按次重试少量几次。
未配置 QUANTDEV_ALERT_WEBHOOK_URL 时静默降级（仅记日志），便于本地/测试运行。
"""
import json
import logging
import threading
import time
import urllib.request
from typing import Dict, Optional

from quantdev.config import settings

logger = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "warning": "🟡", "info": "🟢"}


class AlertService:
    dedup_window_seconds = 300
    max_attempts = 3
    timeout_seconds = 5

    def __init__(self) -> None:
        self._recent: Dict[str, float] = {}
        self._lock = threading.Lock()

    def send(
        self,
        title: str,
        content: str = "",
        severity: str = "high",
        dedup_key: Optional[str] = None,
    ) -> None:
        """发送告警。任何异常都被吞掉，绝不影响调用方主流程。"""
        try:
            self._send(title, content, severity, dedup_key)
        except Exception:  # pragma: no cover - 告警绝不能拖垮交易链路
            logger.exception("告警发送失败（已忽略）")

    def _is_recent(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._recent.get(key)
            return last is not None and now - last < self.dedup_window_seconds

    def _mark_sent(self, key: str) -> None:
        # 仅在“确实发出/确实记录”后登记去重时间戳，避免发送失败后把后续重试也压制掉。
        now = time.time()
        with self._lock:
            self._recent[key] = now
            for stale in [
                k
                for k, ts in self._recent.items()
                if now - ts > self.dedup_window_seconds * 4
            ]:
                self._recent.pop(stale, None)

    def _send(
        self, title: str, content: str, severity: str, dedup_key: Optional[str]
    ) -> None:
        key = dedup_key or title
        if self._is_recent(key):
            return
        emoji = _SEVERITY_EMOJI.get(severity, "")
        text = "{} **{}**\n{}\n\n> env={} severity={}".format(
            emoji, title, content, settings.environment, severity
        )
        url = settings.alert_webhook_url
        if not url:
            logger.warning(
                "ALERT[%s] %s | %s （未配置 QUANTDEV_ALERT_WEBHOOK_URL，仅记录）",
                severity, title, content,
            )
            self._mark_sent(key)
            return
        payload = json.dumps(
            {"msgtype": "markdown", "markdown": {"content": text}}
        ).encode("utf-8")
        last_exc: Optional[Exception] = None
        for _attempt in range(self.max_attempts):
            try:
                request = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                    resp.read()
                self._mark_sent(key)  # 发送成功才登记去重
                return
            except Exception as exc:  # 重试
                last_exc = exc
        if last_exc is not None:
            raise last_exc


alert_service = AlertService()

_installed = False
_install_lock = threading.Lock()


def install_event_alerts() -> None:
    """把告警 handler 订阅到事件总线（幂等：重复调用不会重复订阅）。

    需在每个会发布事件的进程里调用一次：API 进程、worker、paper/live runner。
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        from quantdev.services.events import EventType, event_bus

        def on_kill_switch(event: Dict) -> None:
            payload = event.get("payload", {})
            alert_service.send(
                "Kill Switch 已激活（实盘已停盘）",
                "原因：{}".format(payload.get("reason") or "未说明"),
                severity="critical",
                dedup_key="kill_switch_activated",
            )

        def on_recon_break(event: Dict) -> None:
            payload = event.get("payload", {})
            alert_service.send(
                "对账差异 BREAK",
                "差异 {} 项，现金差 {}，broker={}".format(
                    payload.get("break_count"),
                    payload.get("cash_diff"),
                    payload.get("broker_mode"),
                ),
                severity="critical",
                dedup_key="reconciliation_break",
            )

        def on_order_rejected(event: Dict) -> None:
            payload = event.get("payload", {})
            alert_service.send(
                "实盘订单被拒",
                "{}：{}".format(event.get("aggregate_id"), payload.get("reason")),
                severity="high",
                dedup_key="order_rejected:{}".format(event.get("aggregate_id")),
            )

        event_bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, on_kill_switch)
        event_bus.subscribe(EventType.RECONCILIATION_BREAK, on_recon_break)
        event_bus.subscribe(EventType.ORDER_REJECTED, on_order_rejected)
        _installed = True
