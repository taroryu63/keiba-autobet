"""スマホへの通知。ntfy（無料のプッシュ通知アプリ）にHTTPで送る。"""
from __future__ import annotations

import base64
import logging
import urllib.request

from .config import config

log = logging.getLogger(__name__)


def send(title: str, body: str, priority: str = "default") -> None:
    if not config.ntfy_url:
        log.info("通知（未設定のため送信なし）: %s / %s", title, body)
        return
    req = urllib.request.Request(
        config.ntfy_url,
        data=body.encode("utf-8"),
        headers={
            # ヘッダーはASCIIのみ使えるので、日本語タイトルはRFC 2047形式にする
            "Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=",
            "Priority": priority,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # 通知の失敗で投票処理を止めない
        log.warning("通知の送信に失敗: %s", e)
