"""Bounded informational-popup checks sharing the driver's single UI owner."""
from __future__ import annotations

import asyncio
import time

from civ_arena.game.civ6 import ui_popups


class PopupMonitor:
    def __init__(self, adapter, controller, audit, *, interval=5.0,
                 timeout=180.0, attempts=8):
        self.adapter, self.controller, self.audit = adapter, controller, audit
        self.interval, self.timeout, self.limit = interval, timeout, attempts
        self.lock = asyncio.Lock()
        self.pending = None
        self.started = None
        self.attempts = 0
        self.checks = 0
        self.sent = 0
        self.observed_dismissals = 0

    async def action(self, **kwargs):
        # Recovery keys and the popup monitor never send competing UI actions.
        async with self.lock:
            return await self.controller.action(**kwargs)

    async def quiesce(self):
        async with self.lock:
            pass

    def _record_check(self, result):
        self.checks += 1
        if result["status"] == "sent" or result.get("input", {}).get("status") == "sent":
            self.sent += 1
        self.audit("popup_check", result=result)

    async def check(self, active=lambda: True):
        async with self.lock:
            if not active():
                return False
            if self.pending is not None:
                if self.attempts >= self.limit:
                    raise RuntimeError(f"popup dismissal exhausted for {self.pending}")
                if time.monotonic() - self.started >= self.timeout:
                    raise TimeoutError(f"popup dismissal deadline for {self.pending}")
            remaining = (self.timeout if self.started is None else
                         self.timeout - (time.monotonic() - self.started))
            checked_at = time.monotonic()
            try:
                async with asyncio.timeout(min(10.0, remaining)):
                    result = await ui_popups.dismiss_one(self.adapter, controller=self.controller)
            except (asyncio.CancelledError, TimeoutError) as exc:
                receipt = (getattr(exc, "popup_outcome", None)
                           or getattr(exc.__cause__, "popup_outcome", None))
                if receipt is not None:
                    self._record_check(receipt)
                raise
            self._record_check(result)
            if result["status"] == "failed":
                raise RuntimeError(f"informational popup helper failed: {result['diagnostics']}")
            if result["status"] == "sent":
                popup = result["popup"]
                if popup != self.pending:
                    self.pending, self.started, self.attempts = popup, checked_at, 0
                self.attempts += 1
                if result.get("observed_dismissal"):
                    self.observed_dismissals += 1
                    self.pending, self.started, self.attempts = None, None, 0
                print(f"popup {popup}: close sent; "
                      f"hidden={bool(result.get('observed_dismissal'))}", flush=True)
            elif result["status"] in ("no_target", "skipped_fake"):
                self.pending, self.started, self.attempts = None, None, 0
            else:
                raise RuntimeError("invalid informational popup outcome")
            return result["status"] == "sent"

    async def watch(self, active):
        if self.adapter._simulate is not None:
            return  # fake games never inspect the desktop or the real UI wire
        while True:
            if active():
                await self.check(active)
            await asyncio.sleep(self.interval)

    def summary(self):
        return {"enabled": True, "fake": self.adapter._simulate is not None,
                "interval_s": self.interval, "checks": self.checks,
                "close_requests": self.sent,
                "observed_dismissals": self.observed_dismissals,
                "pending_popup": self.pending, "pending_attempts": self.attempts,
                "allowlist": list(ui_popups.POPUPS)}
