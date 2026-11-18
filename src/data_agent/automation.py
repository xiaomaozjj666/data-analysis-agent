"""定时分析任务：调度、指标异常检测与告警推送（最小闭环）。

来源：2026-11 竞品实地复核（docs/competitive-analysis.md 第六节）确认三家
一手能力——WorkBuddy 的周期任务、Trae 的 Automation、Tableau Pulse 的
"指标洞察推送给关注者"。本项目已有可执行指标与确定性配方，这里补上
"数据找人"的最短路径。

设计取舍：
- **不引入调度框架**：定时由外部调度器（cron / Windows 任务计划）调用
  ``run-due``，或前台 ``loop`` 模式；进程内不起守护线程。
- **异常检测只用确定性统计**：历史窗口上的 z-score 与环比变化，不调用 LLM。
  样本不足（默认 <3）不下结论；基线为 0 时退化为"从 0 起跳"的绝对增量提示。
- **推送失败不丢告警**：历史与告警先落盘，再尝试 webhook；发送失败把错误
  写回状态，而不是让整个任务失败。
- **口径可追溯**：每次运行按**加载时读到的同一份字节**计算指标定义哈希，
  历史里只与相同哈希的样本比较；口径一改，基线自动重置，避免新旧口径混比。
- **单任务失败隔离**：一个任务的数据坏了只影响它自己；失败会记录
  ``last_status=error`` 与重试时间，不会让轮询每 60 秒空转重试。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import smtplib
import socket
import ssl
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http import client as http_client
from pathlib import Path
from typing import Any

from data_agent._workspace_bundles import _atomic_write_text
from data_agent.metrics import compute_metrics_on_frame, parse_metric_definitions
from data_agent.workspace import DataWorkspace

_log = logging.getLogger(__name__)

AUTOMATION_DIRNAME = "automation"
SPECS_FILENAME = "automations.json"
HISTORY_FILENAME = "history.jsonl"
ALERTS_FILENAME = "alerts.jsonl"
STATE_FILENAME = "state.json"
LOCK_FILENAME = ".lock"
DEFAULT_HISTORY_LIMIT = 30
DEFAULT_MIN_SAMPLES = 3
WEBHOOK_TIMEOUT_SECONDS = 5.0
#: 追加式 JSONL 的轮转阈值：超过则把旧文件改名为 ``*.1``（只保留一代）。
#: history/alerts 是 append-only，长期 loop 运行会无限增长——history_limit
#: 只限制读取窗口，不限制文件体积（2026-11 实测告警文件无轮转）。
JSONL_ROTATE_BYTES = 2 * 1024 * 1024
#: 失败后的重试间隔：避免 loop 模式每秒重试坏任务，也避免整批被一个坏任务拖住。
RETRY_BACKOFF_MINUTES = 5

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_SMS_RE = re.compile(r"^\+?[0-9]{7,15}$")

#: 投递渠道的凭据只从环境变量读，不落任务定义（避免 automations.json 泄密）。
SMTP_URL_ENV = "DATA_AGENT_SMTP_URL"
SMTP_FROM_ENV = "DATA_AGENT_SMTP_FROM"
TWILIO_SID_ENV = "DATA_AGENT_TWILIO_ACCOUNT_SID"
TWILIO_TOKEN_ENV = "DATA_AGENT_TWILIO_AUTH_TOKEN"
TWILIO_FROM_ENV = "DATA_AGENT_TWILIO_FROM"
DELIVERY_TIMEOUT_SECONDS = 10.0
_PRIVATE_FLAGS = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_multicast",
    "is_reserved",
    "is_unspecified",
)


@dataclass(frozen=True)
class AutomationSpec:
    """一条定时分析任务。"""

    id: str
    name: str
    dataset_path: str
    metrics_path: str = ""
    interval_minutes: int = 0
    daily_at: str = ""
    metric_names: tuple[str, ...] = ()
    zscore_threshold: float = 3.0
    pct_change_threshold: float = 0.2
    history_limit: int = DEFAULT_HISTORY_LIMIT
    min_samples: int = DEFAULT_MIN_SAMPLES
    webhook_url: str = ""
    #: 邮件收件人（SMTP 连接信息放环境变量，任务定义里不保存凭据）。
    email_to: tuple[str, ...] = ()
    #: 短信收件人（E.164 格式；Twilio 凭据放环境变量）。
    sms_to: tuple[str, ...] = ()
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metric_names"] = list(self.metric_names)
        payload["email_to"] = list(self.email_to)
        payload["sms_to"] = list(self.sms_to)
        return payload


# ---------------------------------------------------------------------------
# webhook 安全
# ---------------------------------------------------------------------------


def _unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(getattr(address, flag) for flag in _PRIVATE_FLAGS)


def _unsafe_host_reason(host: str, *, resolve: bool = True) -> str | None:
    """返回主机不安全的原因；安全或无法判断时返回 None。

    ``resolve=False`` 只检查 IP 字面量，用于解析/登记阶段（不做网络 I/O）；
    发送阶段用 ``resolve=True`` 再查一次 DNS，并逐跳校验重定向。注意：这里
    不做 IP 钉扎，解析与连接仍是两次解析——这是已知边界，文档中明确写出，
    不宣称能完全防御 DNS rebinding。
    """
    candidate = host.strip().strip("[]").lower()
    if not candidate:
        return "主机为空"
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is not None:
        return "目标是内网/环回/保留地址" if _unsafe_address(address) else None
    if not resolve:
        return None
    try:
        infos = socket.getaddrinfo(candidate, None)
    except OSError:
        return None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _unsafe_address(address):
            return "域名解析到内网/环回/保留地址"
    return None


def validate_webhook_url(url: str, *, resolve: bool = True) -> str | None:
    """校验 webhook 地址；返回错误文本，合法时返回 None。"""
    value = (url or "").strip()
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return "webhook 只支持 http/https"
    if parsed.username or parsed.password:
        return "webhook 地址不能包含账号密码"
    if not parsed.hostname:
        return "webhook 地址缺少主机名"
    return _unsafe_host_reason(parsed.hostname, resolve=resolve)


def _validated_sockaddrs(url: str) -> list[tuple[int, tuple[str, int]]]:
    """解析并校验 URL 主机的全部地址；任一命中私网/保留段即整体拒绝。

    与「先 validate 再连接」的两段式不同，这里**一次 getaddrinfo 同时完成
    校验与钉扎**：连接用的就是这批已校验地址，解析与连接之间不再有
    DNS rebinding 窗口（2026-11 评审 I2）。每跳重定向都重新走一遍。
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError as exc:
        raise urllib.error.HTTPError(
            url, 403, f"redirect blocked: 域名解析失败（{exc}）", {}, None
        ) from exc
    safe: list[tuple[int, tuple[str, int]]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        address = ipaddress.ip_address(sockaddr[0])
        if _unsafe_address(address):
            raise urllib.error.HTTPError(
                url, 403, "redirect blocked: 域名解析到内网/环回/保留地址", {}, None
            )
        safe.append((family, (str(address), sockaddr[1])))
    if not safe:
        raise urllib.error.HTTPError(url, 403, "redirect blocked: 域名没有可用地址", {}, None)
    return safe


class _PinnedHTTPSConnection(http_client.HTTPSConnection):
    """连接到已校验 IP，但 TLS 握手/SNI/证书校验仍针对原域名。"""

    def __init__(self, real_host: str, sockaddr: tuple[str, int], timeout: float) -> None:
        super().__init__(sockaddr[0], sockaddr[1], timeout=timeout)
        self._real_host = real_host

    def connect(self) -> None:  # noqa: D102 - 覆写内置行为
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.timeout, source_address=self.source_address
        )
        if self._tunnel_host:
            # 不支持代理隧道：webhook 场景没有代理配置，走直连。
            raise urllib.error.HTTPError(
                f"https://{self._real_host}", 400, "proxy tunnel is not supported", {}, None
            )
        self.sock = self._context.wrap_socket(sock, server_hostname=self._real_host)


class _WebhookResponse:
    """供 ``with`` 使用的最小响应：只暴露 status/code（webhook 不读响应体）。"""

    __slots__ = ("status", "code")

    def __init__(self, status: int) -> None:
        self.status = status
        self.code = status

    def __enter__(self) -> _WebhookResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_WEBHOOK_MAX_HOPS = 5


def _pinned_send(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
) -> int:
    """逐跳校验 + IP 钉扎发送 webhook 请求，返回最终 HTTP 状态码。

    每一跳：语法/凭据校验（resolve=False）→ 一次 getaddrinfo 同时校验并钉扎
    → http.client 直连该 IP（TLS 用原域名做 SNI 与证书校验，Host 头保持原
    域名）。重定向只在白名单语义下跟随，且每跳都重走完整校验。
    """
    current = url
    current_method = method.upper()
    current_body = body
    for _hop in range(_WEBHOOK_MAX_HOPS + 1):
        # 语法层校验不解析 DNS（DNS 由下面的钉扎路径负责，且只解一次）。
        unsafe = validate_webhook_url(current, resolve=False)
        if unsafe:
            raise urllib.error.HTTPError(current, 403, f"redirect blocked: {unsafe}", {}, None)
        parsed = urllib.parse.urlsplit(current)
        is_https = parsed.scheme.lower() == "https"
        sockaddrs = _validated_sockaddrs(current)
        family, (ip, port) = sockaddrs[0]
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        send_headers = {key.lower(): value for key, value in headers.items()}
        send_headers["host"] = parsed.netloc
        if current_body is not None and "content-length" not in send_headers:
            send_headers["content-length"] = str(len(current_body))
        if is_https:
            connection: http_client.HTTPConnection = _PinnedHTTPSConnection(
                parsed.hostname or "", (ip, port), WEBHOOK_TIMEOUT_SECONDS
            )
        else:
            connection = http_client.HTTPConnection(ip, port, timeout=WEBHOOK_TIMEOUT_SECONDS)
        try:
            connection.request(current_method, path, body=current_body, headers=send_headers)
            response = connection.getresponse()
            status = int(response.status)
            location = response.headers.get("Location") if status in _REDIRECT_STATUSES else None
            response.read()
        finally:
            try:
                connection.close()
            except OSError:
                pass
        if status not in _REDIRECT_STATUSES:
            return status
        if not location:
            raise urllib.error.HTTPError(current, status, "redirect without Location", {}, None)
        if status in {301, 302, 303}:
            # 303（以及历史习惯的 301/302）改写为 GET 并丢弃请求体。
            current_method = "GET"
            current_body = None
        current = urllib.parse.urljoin(current, location)
    raise urllib.error.HTTPError(url, 508, "redirect blocked: too many hops", {}, None)


def _post_request(request: urllib.request.Request) -> _WebhookResponse:
    """发送 webhook 请求（独立函数便于测试替换；每跳校验 + IP 钉扎）。"""
    headers = {key: value for key, value in request.header_items()}
    status = _pinned_send(request.get_method(), request.full_url, request.data, headers)
    return _WebhookResponse(status)


def _channel_for_url(webhook_url: str) -> str | None:
    """按 webhook 主机识别常见群机器人（Slack / 飞书 / 钉钉）。"""
    host = (urllib.parse.urlsplit(webhook_url).hostname or "").lower()
    if host == "hooks.slack.com" or host.endswith(".slack.com"):
        return "slack"
    if host in {"open.feishu.cn", "open.larksuite.com"} or host.endswith(".larksuite.com"):
        return "feishu"
    if host in {"oapi.dingtalk.com", "api.dingtalk.com"} or host.endswith(".dingtalk.com"):
        return "dingtalk"
    return None


def _alert_text(payload: dict[str, Any]) -> str:
    """把结构化告警压成群机器人可直接渲染的短文本（有长度上限）。"""
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    alerts = payload.get("alerts") if isinstance(payload.get("alerts"), list) else []
    lines = [
        f"[data-agent] {task.get('name') or task.get('id') or '定时任务'}"
        f" @ {payload.get('ran_at') or ''}".rstrip()
    ]
    attribution = payload.get("attribution")
    if isinstance(attribution, dict) and attribution.get("headline"):
        lines.append(str(attribution["headline"]))
    for alert in alerts[:5]:
        if not isinstance(alert, dict):
            continue
        reasons = "；".join(str(item) for item in alert.get("reasons") or [])
        lines.append(f"- {alert.get('metric')}={alert.get('value')}：{reasons or '触发阈值'}")
    if len(alerts) > 5:
        lines.append(f"…另有 {len(alerts) - 5} 项告警，详见 alerts.jsonl")
    return "\n".join(lines)[:3500]


def _webhook_body(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按目标渠道转换请求体；未识别的地址保持原始结构化 JSON。

    群机器人只接受各自的文本格式，直接 POST 原始 JSON 会 400；识别到已知
    主机时改发文本摘要，避免用户还要自己写一层格式转换。结构化明细仍完整
    保存在 ``alerts.jsonl``，不因渠道压缩而丢失。
    """
    channel = _channel_for_url(webhook_url)
    if channel is None:
        return payload
    text = _alert_text(payload)
    if channel == "slack":
        return {"text": text}
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"msgtype": "text", "text": {"content": text}}


def _notify_result(webhook_url: str, payload: dict[str, Any]) -> tuple[str | None, int | None, int]:
    """POST 告警到 webhook；返回 ``(错误文本, HTTP 状态码, 耗时 ms)``。

    成功时错误为 None。状态码/耗时用于事后判断「推没推出去、推了多久」——
    只有 ok/失败两个字段时，无法区分 4xx（地址/鉴权配错）与 5xx（对方故障）。
    """
    if not webhook_url:
        return None, None, 0
    unsafe = validate_webhook_url(webhook_url)
    if unsafe:
        return f"webhook 被拒绝：{unsafe}", None, 0
    body = json.dumps(_webhook_body(webhook_url, payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with _post_request(request) as response:
            status = getattr(response, "status", None) or getattr(response, "code", None)
            try:
                code = int(status) if status is not None else None
            except (TypeError, ValueError):
                code = None
            return None, code, int((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as exc:
        # HTTPError 是 URLError 子类：必须单独先接，否则 4xx/5xx 的状态码
        # 会在通用分支里丢失（正是最需要区分配置错误与对方故障的时候）。
        return str(exc)[:300], int(exc.code), int((time.monotonic() - started) * 1000)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return str(exc)[:300], None, int((time.monotonic() - started) * 1000)


def _notify(webhook_url: str, payload: dict[str, Any]) -> str | None:
    """POST 告警到 webhook；成功返回 None，失败返回错误文本（不抛异常）。"""
    return _notify_result(webhook_url, payload)[0]


def _parse_recipients(raw: Any, *, field: str, task_id: str) -> tuple[str, ...]:
    """解析 email_to / sms_to；接受数组或逗号/分号分隔字符串。"""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        items = [part.strip() for part in re.split(r"[,;]", raw) if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip() for part in raw if str(part).strip()]
    else:
        raise ValueError(f"任务 {task_id} 的 {field} 必须是字符串数组或逗号分隔字符串。")
    unique = list(dict.fromkeys(items))
    limit = 10 if field == "email_to" else 5
    if len(unique) > limit:
        raise ValueError(f"任务 {task_id} 的 {field} 最多 {limit} 个收件人。")
    pattern = _EMAIL_RE if field == "email_to" else _SMS_RE
    invalid = [item for item in unique if not pattern.fullmatch(item)]
    if invalid:
        hint = "邮箱地址" if field == "email_to" else "E.164 手机号（如 +8613800000000）"
        raise ValueError(f"任务 {task_id} 的 {field} 含非法{hint}：{invalid[0]}")
    return tuple(unique)


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _smtp_config() -> dict[str, Any] | None:
    """从环境变量解析 SMTP 配置；未配置返回 None，配置错误抛 ValueError。"""
    raw = os.environ.get(SMTP_URL_ENV, "").strip()
    if not raw:
        return None
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"smtp", "smtps"}:
        raise ValueError(f"{SMTP_URL_ENV} 只支持 smtp:// 或 smtps://")
    if not parsed.hostname:
        raise ValueError(f"{SMTP_URL_ENV} 缺少主机名")
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    query = urllib.parse.parse_qs(parsed.query)
    use_ssl = parsed.scheme == "smtps"
    starttls = not use_ssl and (query.get("starttls", ["1"])[0].lower() not in {"0", "false"})
    return {
        "host": parsed.hostname,
        "port": parsed.port or (465 if use_ssl else 587),
        "username": username,
        "password": password,
        "ssl": use_ssl,
        "starttls": starttls,
        "from_addr": os.environ.get(SMTP_FROM_ENV, "").strip() or username,
    }


def _send_email_result(
    recipients: tuple[str, ...], subject: str, text: str
) -> tuple[str | None, int | None, int]:
    """发送告警邮件；返回 ``(错误文本, SMTP 状态码, 耗时 ms)``，不抛异常。"""
    if not recipients:
        return None, None, 0
    try:
        config = _smtp_config()
    except ValueError as exc:
        return str(exc), None, 0
    if config is None:
        return f"未配置 {SMTP_URL_ENV}，无法发送邮件。", None, 0
    if not config["from_addr"]:
        return f"缺少发件人：请设置 {SMTP_FROM_ENV} 或在 SMTP URL 中带用户名。", None, 0
    started = time.monotonic()
    try:
        message = EmailMessage()
        # 主题/发件人都含用户可控内容（任务名、SMTP URL 的 username 段可能带
        # %0d%0a）：统一折叠空白，防邮件头注入；EmailMessage 对裸 CRLF 会直接
        # 抛错，归一化保证配置手滑时是「正常发信」而不是「整封失败」。
        message["Subject"] = " ".join(str(subject).split())[:200]
        message["From"] = " ".join(str(config["from_addr"]).split())
        message["To"] = ", ".join(recipients)
        message.set_content(text)
        if config["ssl"]:
            client = smtplib.SMTP_SSL(
                str(config["host"]),
                int(config["port"]),
                timeout=DELIVERY_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                str(config["host"]), int(config["port"]), timeout=DELIVERY_TIMEOUT_SECONDS
            )
        with client:
            if config["starttls"]:
                client.starttls(context=ssl.create_default_context())
            if config["username"]:
                client.login(str(config["username"]), str(config["password"]))
            client.send_message(message)
        return None, 250, int((time.monotonic() - started) * 1000)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        code = getattr(exc, "smtp_code", None)
        reason = _redact(str(exc), str(config["password"]))[:300]
        return (
            reason,
            int(code) if isinstance(code, int) else None,
            int((time.monotonic() - started) * 1000),
        )


def _twilio_config() -> dict[str, str] | None:
    sid = os.environ.get(TWILIO_SID_ENV, "").strip()
    token = os.environ.get(TWILIO_TOKEN_ENV, "").strip()
    sender = os.environ.get(TWILIO_FROM_ENV, "").strip()
    if not sid and not token and not sender:
        return None
    missing = [
        name
        for name, value in (
            (TWILIO_SID_ENV, sid),
            (TWILIO_TOKEN_ENV, token),
            (TWILIO_FROM_ENV, sender),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Twilio 配置不完整：缺少 {', '.join(missing)}")
    return {"sid": sid, "token": token, "from": sender}


def _send_sms_result(recipients: tuple[str, ...], text: str) -> tuple[str | None, int | None, int]:
    """通过 Twilio REST API 发送短信；返回 ``(错误文本, HTTP 状态码, 耗时 ms)``。"""
    if not recipients:
        return None, None, 0
    try:
        config = _twilio_config()
    except ValueError as exc:
        return str(exc), None, 0
    if config is None:
        return f"未配置 Twilio（{TWILIO_SID_ENV}/{TWILIO_TOKEN_ENV}/{TWILIO_FROM_ENV}）。", None, 0
    started = time.monotonic()
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(config['sid'])}/Messages.json"
    auth = base64.b64encode(f"{config['sid']}:{config['token']}".encode()).decode("ascii")
    body = text[:300] + ("…" if len(text) > 300 else "")
    statuses: list[int] = []
    errors: list[str] = []
    for recipient in recipients:
        form = urllib.parse.urlencode(
            {"From": config["from"], "To": recipient, "Body": body}
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth}",
            },
            method="POST",
        )
        try:
            with _post_request(request) as response:
                status = getattr(response, "status", None) or getattr(response, "code", None)
                statuses.append(int(status) if status is not None else 201)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, int):
                statuses.append(code)
            errors.append(_redact(str(exc), config["token"], config["sid"])[:200])
    elapsed = int((time.monotonic() - started) * 1000)
    if errors:
        return "；".join(errors)[:300], statuses[-1] if statuses else None, elapsed
    return None, statuses[-1] if statuses else None, elapsed


# ---------------------------------------------------------------------------
# 定义解析与存储
# ---------------------------------------------------------------------------


def parse_spec(payload: Any) -> AutomationSpec:
    """校验并解析一条任务定义；结构不对时报错而不是跳过。"""
    if not isinstance(payload, dict):
        raise ValueError("automation 任务必须是 JSON 对象。")
    task_id = str(payload.get("id") or "").strip()
    if not _ID_RE.match(task_id):
        raise ValueError("id 只能包含字母、数字、下划线或连字符（1–64 位）。")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"任务 {task_id} 缺少 name。")
    dataset = str(payload.get("dataset_path") or "").strip()
    if not dataset:
        raise ValueError(f"任务 {task_id} 缺少 dataset_path。")
    interval_raw = payload.get("interval_minutes", 0)
    try:
        interval = int(interval_raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"任务 {task_id} 的 interval_minutes 必须是整数。") from exc
    daily_at = str(payload.get("daily_at") or "").strip()
    if bool(interval) == bool(daily_at):
        raise ValueError(f"任务 {task_id} 必须且只能设置 interval_minutes 或 daily_at 之一。")
    if interval and interval < 1:
        raise ValueError(f"任务 {task_id} 的 interval_minutes 必须 ≥ 1。")
    if daily_at and not _TIME_RE.match(daily_at):
        raise ValueError(f"任务 {task_id} 的 daily_at 必须是 HH:MM（24 小时制）。")
    try:
        zscore = float(payload.get("zscore_threshold", 3.0))
        pct = float(payload.get("pct_change_threshold", 0.2))
        history_limit = int(payload.get("history_limit", DEFAULT_HISTORY_LIMIT))
        min_samples = int(payload.get("min_samples", DEFAULT_MIN_SAMPLES))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"任务 {task_id} 的阈值/窗口参数必须是数字。") from exc
    if not math.isfinite(zscore) or zscore <= 0:
        raise ValueError(f"任务 {task_id} 的 zscore_threshold 必须为正数。")
    if not math.isfinite(pct) or pct <= 0:
        raise ValueError(f"任务 {task_id} 的 pct_change_threshold 必须为正数。")
    if history_limit < 1 or min_samples < 1:
        raise ValueError(f"任务 {task_id} 的 history_limit / min_samples 必须 ≥ 1。")
    if min_samples > history_limit:
        raise ValueError(f"任务 {task_id} 的 min_samples 不能大于 history_limit。")
    metric_names = payload.get("metric_names") or []
    if not isinstance(metric_names, (list, tuple)):
        raise ValueError(f"任务 {task_id} 的 metric_names 必须是字符串数组。")
    names = tuple(str(item).strip() for item in metric_names if str(item).strip())
    webhook_url = str(payload.get("webhook_url") or "").strip()
    # 登记阶段不做 DNS（纯解析不应触发网络 I/O，也不能因域名当前解析到内网
    # 就让整份任务列表加载失败）；只拦明确不安全的 scheme/账号/IP 字面量。
    webhook_error = validate_webhook_url(webhook_url, resolve=False)
    if webhook_error:
        raise ValueError(f"任务 {task_id} 的 webhook_url 不安全：{webhook_error}。")
    email_to = _parse_recipients(payload.get("email_to"), field="email_to", task_id=task_id)
    sms_to = _parse_recipients(payload.get("sms_to"), field="sms_to", task_id=task_id)
    return AutomationSpec(
        id=task_id,
        name=name,
        dataset_path=dataset,
        metrics_path=str(payload.get("metrics_path") or "").strip(),
        interval_minutes=interval,
        daily_at=daily_at,
        metric_names=names,
        zscore_threshold=zscore,
        pct_change_threshold=pct,
        history_limit=history_limit,
        min_samples=min_samples,
        webhook_url=webhook_url,
        email_to=email_to,
        sms_to=sms_to,
        enabled=bool(payload.get("enabled", True)),
    )


def automation_root(runs_dir: str | Path) -> Path:
    return Path(runs_dir).expanduser() / AUTOMATION_DIRNAME


def specs_path(runs_dir: str | Path) -> Path:
    return automation_root(runs_dir) / SPECS_FILENAME


def task_dir(runs_dir: str | Path, task_id: str) -> Path:
    return automation_root(runs_dir) / task_id


def load_specs(runs_dir: str | Path) -> list[AutomationSpec]:
    path = specs_path(runs_dir)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("automations")
    if not isinstance(payload, list):
        raise ValueError(f"{path} 必须是数组或包含 automations 数组。")
    return [parse_spec(item) for item in payload]


def save_specs(runs_dir: str | Path, specs: list[AutomationSpec]) -> None:
    path = specs_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"automations": [spec.to_dict() for spec in specs]}, ensure_ascii=False, indent=2
    )
    _atomic_write_text(path, payload + "\n")


def add_spec(runs_dir: str | Path, spec: AutomationSpec) -> list[AutomationSpec]:
    specs = load_specs(runs_dir)
    merged: list[AutomationSpec] = []
    replaced = False
    for item in specs:
        if item.id == spec.id:
            merged.append(spec)
            replaced = True
        else:
            merged.append(item)
    if not replaced:
        merged.append(spec)
    save_specs(runs_dir, merged)
    return merged


def remove_spec(runs_dir: str | Path, task_id: str) -> bool:
    specs = load_specs(runs_dir)
    remaining = [item for item in specs if item.id != task_id]
    if len(remaining) == len(specs):
        return False
    save_specs(runs_dir, remaining)
    return True


def _read_state(runs_dir: str | Path, task_id: str) -> dict[str, Any]:
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(runs_dir: str | Path, task_id: str, state: dict[str, Any]) -> None:
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _state_corrupt(runs_dir: str | Path, task_id: str) -> bool:
    """state.json 是否存在但无法解析（读侧按「从未运行」降级，但要留痕）。"""
    path = task_dir(runs_dir, task_id) / STATE_FILENAME
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not isinstance(payload, dict)


def _append_jsonl(path: Path, payload: str, *, rotate_bytes: int = JSONL_ROTATE_BYTES) -> None:
    """追加一行 JSONL；文件超过阈值时先轮转成 ``*.1``（只保留一代）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.stat().st_size > rotate_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError as exc:
        _log.warning("could not rotate %s: %s", path.name, exc)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload + "\n")


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_due(
    spec: AutomationSpec,
    last_run_at: str,
    now: datetime,
    *,
    next_retry_at: str = "",
) -> bool:
    """判断任务此刻是否到期；坏状态按"从未运行"处理，不阻断整批。"""
    if not spec.enabled:
        return False
    retry = _parse_time(next_retry_at) if next_retry_at else None
    if retry is not None and now < retry:
        return False
    last = _parse_time(last_run_at) if last_run_at else None
    if spec.interval_minutes:
        if last is None:
            return True
        return now - last >= timedelta(minutes=spec.interval_minutes)
    hour, minute = (int(part) for part in spec.daily_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return False
    if last is None:
        return True
    return last < target


def next_due_at(spec: AutomationSpec, last_run_at: str, now: datetime) -> datetime | None:
    """估算下一次到期时刻；停用任务返回 ``None``。

    这是**展示用估算**，不是调度器的承诺：间隔任务按"上次运行 + 间隔"推算，
    每日任务按当天/次日的本地目标时刻推算。DST 切换日仍按本地墙钟时间，
    与 :func:`is_due` 的判定口径一致；错过的任务会返回一个已过去的时刻，
    提醒调用方"现在已经到期"，而不是假装它还在未来。
    """
    if not spec.enabled:
        return None
    last = _parse_time(last_run_at) if last_run_at else None
    if spec.interval_minutes:
        return now if last is None else last + timedelta(minutes=spec.interval_minutes)
    hour, minute = (int(part) for part in spec.daily_at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target or last is None or last < target:
        return target
    return target + timedelta(days=1)


def _coerce_int(value: Any) -> int:
    """把状态里的计数转成 int；坏类型（含 bool/str/None）安全回退 0。"""
    try:
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


def status_overview(runs_dir: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """汇总全部任务的状态，供看板/巡检脚本消费（不触发任何执行）。"""
    moment = now or datetime.now().astimezone()
    tasks: list[dict[str, Any]] = []
    for spec in load_specs(runs_dir):
        state = _read_state(runs_dir, spec.id)
        last_run_at = str(state.get("last_run_at") or "")
        due = is_due(
            spec,
            last_run_at,
            moment,
            next_retry_at=str(state.get("next_retry_at") or ""),
        )
        next_due = next_due_at(spec, last_run_at, moment)
        tasks.append(
            {
                "id": spec.id,
                "name": spec.name,
                "enabled": spec.enabled,
                "schedule": (
                    {"interval_minutes": spec.interval_minutes}
                    if spec.interval_minutes
                    else {"daily_at": spec.daily_at}
                ),
                "due": due,
                "next_due_at": next_due.isoformat(timespec="seconds") if next_due else None,
                "last_run_at": last_run_at or None,
                "last_status": state.get("last_status"),
                # 手改的 state.json 可能是合法 JSON 但类型错误：坏类型按 0 处理，
                # 别让一个任务的怪数据把整个看板/巡检打挂。
                "last_alerts": _coerce_int(state.get("last_alerts")),
                "last_alert_headline": state.get("last_alert_headline"),
                "last_error": state.get("last_error"),
                "last_webhook_status": state.get("last_webhook_status"),
                "next_retry_at": state.get("next_retry_at"),
                "state_corrupt": _state_corrupt(runs_dir, spec.id),
            }
        )
    summary = {
        "total": len(tasks),
        "enabled": sum(1 for item in tasks if item["enabled"]),
        "due": sum(1 for item in tasks if item["due"]),
        "alerts": sum(item["last_alerts"] for item in tasks),
        "errors": sum(1 for item in tasks if item["last_status"] == "error"),
    }
    return {
        "generated_at": moment.isoformat(timespec="seconds"),
        "summary": summary,
        "tasks": tasks,
    }


def read_history(runs_dir: str | Path, task_id: str, limit: int) -> list[dict[str, Any]]:
    path = task_dir(runs_dir, task_id) / HISTORY_FILENAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
    if skipped:
        # 坏行静默丢弃会让「历史上限」看起来莫名变小；留 warning 便于发现截断。
        _log.warning(
            "history for task %s contains %d unparsable line(s); they were ignored",
            task_id,
            skipped,
        )
    return rows[-limit:] if limit > 0 else rows


def _lock_handle(handle: Any) -> bool:  # pragma: no cover - 平台分支
    """尝试对打开的文件加非阻塞排他锁（Windows / POSIX 两套 API）。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_handle(handle: Any) -> None:  # pragma: no cover - 平台分支
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _task_lock(runs_dir: str | Path, task_id: str):
    """任务级跨进程非阻塞锁；拿不到锁时 yield False。"""
    directory = task_dir(runs_dir, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_FILENAME
    handle = path.open("a+")
    acquired = _lock_handle(handle)
    try:
        yield acquired
    finally:
        if acquired:
            _unlock_handle(handle)
        handle.close()


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------


def detect_anomalies(
    current: dict[str, float],
    history: list[dict[str, Any]],
    spec: AutomationSpec,
) -> list[dict[str, Any]]:
    """在当前值与历史窗口上检测异常（z-score / 环比 / 零基线起跳）。"""
    alerts: list[dict[str, Any]] = []
    for metric, value in current.items():
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric_value):
            continue
        previous = [
            float(row["metrics"][metric])
            for row in history
            if isinstance(row.get("metrics"), dict)
            and isinstance(row["metrics"].get(metric), (int, float))
            and math.isfinite(float(row["metrics"][metric]))
        ]
        if len(previous) < spec.min_samples:
            continue
        mean = statistics.fmean(previous)
        stdev = statistics.pstdev(previous)
        zscore = (numeric_value - mean) / stdev if stdev > 0 else None
        last = previous[-1]
        pct_change = (numeric_value - last) / abs(last) if last != 0 else None
        reasons: list[str] = []
        if zscore is not None and abs(zscore) >= spec.zscore_threshold:
            reasons.append(f"z-score {zscore:+.2f} ≥ {spec.zscore_threshold:g}")
        if pct_change is not None and abs(pct_change) >= spec.pct_change_threshold:
            reasons.append(f"环比 {pct_change:+.1%} ≥ {spec.pct_change_threshold:.0%}")
        if last == 0 and abs(numeric_value) > 1e-9:
            # 零基线：方差为 0、环比无定义，但从 0 起跳本身就是需要关注的变化。
            reasons.append("基线为 0，当前值非零")
        if reasons:
            # 阈值归一化后的严重度：z-score 与环比各自除以自己的阈值再取大，
            # 这样「z=4 / 阈值 3」与「环比 60% / 阈值 20%」可以直接比大小，
            # 不会因为两个指标量纲不同而总让其中一种原因霸榜。
            ratios: list[float] = []
            if zscore is not None:
                ratios.append(abs(zscore) / spec.zscore_threshold)
            if pct_change is not None:
                ratios.append(abs(pct_change) / spec.pct_change_threshold)
            if last == 0 and abs(numeric_value) > 1e-9:
                # 零基线没有方差与环比，按「刚过阈值」计，至少不排在正常项后面。
                ratios.append(1.0)
            # 方向优先按环比符号——headline 展示的首选口径就是环比；没有环比
            # （历史不足/零基线）才退回与基线均值比较，避免出现「上升（环比
            # -50%）」这种自相矛盾的摘要。
            if pct_change is not None:
                direction = "up" if pct_change > 0 else "down" if pct_change < 0 else "flat"
            else:
                direction = (
                    "up" if numeric_value > mean else "down" if numeric_value < mean else "flat"
                )
            alerts.append(
                {
                    "metric": metric,
                    "value": numeric_value,
                    "baseline_mean": mean,
                    "baseline_stdev": stdev,
                    "zscore": zscore,
                    "pct_change": pct_change,
                    "direction": direction,
                    "severity": round(max(ratios, default=1.0), 4),
                    "reasons": reasons,
                }
            )
    return alerts


def summarize_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """把多条告警收敛成一段可读的归因摘要（不调用 LLM，全部可复算）。

    输出回答三个问题：哪项偏离最大、偏离方向是什么、是不是所有指标一起动。
    最后一项有实际诊断价值——全部同向通常意味着数据源/口径整体变化，单项
    偏离更可能是该指标自身的业务事件；但这里只做排序与方向统计，不臆断原因。
    """
    if not alerts:
        return None
    ranked = sorted(
        alerts,
        key=lambda item: (
            -float(item.get("severity") or 0.0),
            -abs(float(item.get("value") or 0.0)),
        ),
    )
    top = ranked[0]
    detail: list[str] = []
    if top.get("pct_change") is not None:
        detail.append(f"环比 {float(top['pct_change']):+.1%}")
    if top.get("zscore") is not None:
        detail.append(f"z-score {float(top['zscore']):+.2f}")
    if not detail:
        detail.append("超出设定阈值")
    label = {"up": "上升", "down": "下降", "flat": "持平"}.get(
        str(top.get("direction") or ""), "变化"
    )
    headline = f"{top.get('metric')} {label}（{'、'.join(detail)}），为本次偏离最大的指标"
    directions = {"up": 0, "down": 0, "flat": 0}
    for item in alerts:
        key = str(item.get("direction") or "")
        if key in directions:
            directions[key] += 1
    has_up = directions["up"] > 0
    has_down = directions["down"] > 0
    return {
        "headline": headline,
        "top_metric": top.get("metric"),
        "max_severity": top.get("severity"),
        "count": len(alerts),
        # 「所有告警同向」要求：确有移动、且只有一个方向、没有持平项。
        # [up, flat] 或全 flat 都不算同向（此前 flat 被排除在 moved 外，
        # 会误报同向）。
        "same_direction": (has_up != has_down) and directions["flat"] == 0,
        "directions": directions,
        "ranking": [
            {
                "metric": item.get("metric"),
                "severity": item.get("severity"),
                "direction": item.get("direction"),
                "value": item.get("value"),
                "baseline_mean": item.get("baseline_mean"),
            }
            for item in ranked[:10]
        ],
    }


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------


def _metrics_snapshot(metrics_path: Path) -> tuple[str, list[Any]]:
    """读取指标定义并返回 ``(sha256, definitions)``，两者来自同一份字节。"""
    raw = metrics_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    return digest, parse_metric_definitions(payload)


def _make_workspace(
    spec: AutomationSpec, runs_dir: str | Path, dry_run: bool
) -> tuple[DataWorkspace, Path | None]:
    if dry_run:
        base = Path(tempfile.mkdtemp(prefix="data-agent-automation-dry-"))
        return DataWorkspace(base, session_id=spec.id), base
    return (
        DataWorkspace(Path(runs_dir).expanduser() / "automation_workspaces", session_id=spec.id),
        None,
    )


def run_automation(
    spec: AutomationSpec,
    runs_dir: str | Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
) -> dict[str, Any]:
    """执行一次任务：加载数据 → 计算登记指标 → 检测异常 → 落历史/告警/推送。"""
    moment = now or datetime.now().astimezone()
    metrics_path = Path(spec.metrics_path).expanduser() if spec.metrics_path else None
    if metrics_path is None:
        candidate = Path(spec.dataset_path).expanduser().parent / "metrics.json"
        metrics_path = candidate if candidate.is_file() else None
    if metrics_path is None:
        raise ValueError(
            f"任务 {spec.id} 没有可用的指标定义（metrics_path 或数据集同目录 metrics.json）。"
        )
    metrics_sha256, definitions = _metrics_snapshot(metrics_path)
    if spec.metric_names:
        allowed = {name.casefold() for name in spec.metric_names}
        definitions = [item for item in definitions if item.name.casefold() in allowed]
        if not definitions:
            raise ValueError(f"任务 {spec.id} 指定的指标在定义文件中不存在。")

    if dry_run:
        workspace, temp_root = _make_workspace(spec, runs_dir, True)
        try:
            return _run_with_workspace(
                workspace,
                spec,
                runs_dir,
                moment,
                metrics_path,
                metrics_sha256,
                definitions,
                dry_run=True,
                notify=False,
            )
        finally:
            if temp_root is not None:
                shutil.rmtree(temp_root, ignore_errors=True)

    with _task_lock(runs_dir, spec.id) as acquired:
        if not acquired:
            return {"id": spec.id, "name": spec.name, "skipped": "locked"}
        workspace, _ = _make_workspace(spec, runs_dir, False)
        return _run_with_workspace(
            workspace,
            spec,
            runs_dir,
            moment,
            metrics_path,
            metrics_sha256,
            definitions,
            dry_run=False,
            notify=notify,
        )


def render_dashboard(overview: dict[str, Any], *, refresh_seconds: int = 0) -> str:
    """把 :func:`status_overview` 渲染成零依赖的单文件 HTML 看板。

    看板是只读快照：不引入前端构建链、不依赖后端在线；`refresh_seconds>0`
    时用 meta refresh 定时重载（需要重新生成文件才能看到新数据）。所有任务
    字段都经过 HTML 转义——任务名来自用户配置，不能当可信 HTML。
    """
    from html import escape

    summary = overview.get("summary") if isinstance(overview.get("summary"), dict) else {}
    tasks = overview.get("tasks") if isinstance(overview.get("tasks"), list) else []
    refresh = max(0, int(refresh_seconds))
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    cards = "".join(
        f'<div class="card"><span>{escape(label)}</span><strong>{escape(str(value))}</strong></div>'
        for label, value in (
            ("任务总数", summary.get("total", 0)),
            ("启用", summary.get("enabled", 0)),
            ("已到期", summary.get("due", 0)),
            ("上次告警", summary.get("alerts", 0)),
            ("失败任务", summary.get("errors", 0)),
        )
    )
    rows: list[str] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        status = str(item.get("last_status") or "never")
        due = bool(item.get("due"))
        css = "due" if due else status if status in {"ok", "partial", "error"} else "never"
        rows.append(
            "<tr>"
            f"<td><strong>{escape(str(item.get('name') or ''))}</strong><br>"
            f"<code>{escape(str(item.get('id') or ''))}</code></td>"
            f'<td><span class="chip {css}">{escape("到期" if due else status)}</span></td>'
            f"<td>{escape(str(item.get('last_run_at') or '从未运行'))}</td>"
            f"<td>{escape(str(item.get('next_due_at') or '—'))}</td>"
            f"<td>{escape(str(item.get('last_alert_headline') or '—'))}</td>"
            f"<td>{escape(str(item.get('last_error') or '—'))}</td>"
            "</tr>"
        )
    body_rows = "".join(rows) or '<tr><td colspan="6" class="empty">暂无任务</td></tr>'
    generated = escape(str(overview.get("generated_at") or ""))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>data-agent 定时任务看板</title>
<style>
body{{margin:0;padding:32px;background:#0b1020;color:#e5e7eb;font:15px/1.6 system-ui,"Segoe UI",sans-serif}}
h1{{margin:0 0 4px;font-size:24px}} .muted{{color:#94a3b8;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:#151b2e;border:1px solid #243049;border-radius:12px;padding:14px}}
.card span{{color:#94a3b8;font-size:13px}} .card strong{{display:block;font-size:26px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#151b2e;border-radius:12px;overflow:hidden}}
th,td{{text-align:left;padding:12px 14px;border-bottom:1px solid #243049;vertical-align:top}}
th{{color:#94a3b8;font-weight:600;font-size:13px}} tr:last-child td{{border-bottom:0}}
code{{color:#93c5fd;font-size:12px}}
.chip{{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;background:#334155}}
.chip.ok{{background:#14532d;color:#bbf7d0}} .chip.partial{{background:#713f12;color:#fef08a}}
.chip.error{{background:#7f1d1d;color:#fecaca}} .chip.due{{background:#1e3a8a;color:#bfdbfe}}
.chip.never{{background:#334155;color:#cbd5e1}} .empty{{text-align:center;color:#64748b}}
</style></head><body>
<h1>data-agent 定时任务看板</h1>
<div class="muted">生成于 {generated} · 只读快照</div>
<div class="cards">{cards}</div>
<table><thead><tr><th>任务</th><th>状态</th><th>上次运行</th><th>下次预计</th><th>最近归因</th><th>最近错误</th></tr></thead>
<tbody>{body_rows}</tbody></table>
</body></html>
"""


def _run_with_workspace(
    workspace: DataWorkspace,
    spec: AutomationSpec,
    runs_dir: str | Path,
    moment: datetime,
    metrics_path: Path,
    metrics_sha256: str,
    definitions: list[Any],
    *,
    dry_run: bool,
    notify: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    workspace.load(spec.dataset_path)
    computed = compute_metrics_on_frame(workspace.dataframe, definitions)
    current = {
        item["name"]: float(item["sum"])
        for item in computed
        if item.get("status") == "ok"
        and item.get("sum") is not None
        and int(item.get("non_null") or 0) > 0
    }
    errors = {
        item["name"]: str(item.get("error") or "无有效数值")
        for item in computed
        if item.get("status") != "ok"
        or item.get("sum") is None
        or int(item.get("non_null") or 0) == 0
    }
    history = [
        row
        for row in read_history(runs_dir, spec.id, spec.history_limit)
        # 只与相同口径版本的历史比较；旧行没有 hash 时按同版本兼容。
        if row.get("metrics_sha256") in (None, metrics_sha256)
    ]
    alerts = detect_anomalies(current, history, spec)
    attribution = summarize_alerts(alerts)
    ran_at = moment.isoformat(timespec="seconds")
    state_corrupt = _state_corrupt(runs_dir, spec.id)
    if state_corrupt:
        _log.warning(
            "state.json for task %s is corrupt; treating it as never-run (last_run_at lost)",
            spec.id,
        )
    result: dict[str, Any] = {
        "id": spec.id,
        "name": spec.name,
        "ran_at": ran_at,
        "metrics": computed,
        "metric_errors": errors,
        "alerts": alerts,
        "attribution": attribution,
        "notified": False,
        "webhook_error": None,
        "webhook_status": None,
        "webhook_duration_ms": None,
        "email_sent": False,
        "email_error": None,
        "email_status": None,
        "sms_sent": False,
        "sms_error": None,
        "sms_status": None,
        "dry_run": dry_run,
        "history_samples": len(history),
        "metrics_file": str(metrics_path),
        "metrics_sha256": metrics_sha256,
        "state_corrupt": state_corrupt,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if dry_run:
        return result

    directory = task_dir(runs_dir, spec.id)
    directory.mkdir(parents=True, exist_ok=True)
    _append_jsonl(
        directory / HISTORY_FILENAME,
        json.dumps(
            {
                "at": ran_at,
                "metrics": current,
                "errors": errors,
                "metrics_sha256": metrics_sha256,
                "duration_ms": result["duration_ms"],
            },
            ensure_ascii=False,
        ),
    )
    if alerts:
        _append_jsonl(
            directory / ALERTS_FILENAME,
            json.dumps(
                {
                    "at": ran_at,
                    "task_id": spec.id,
                    "alerts": alerts,
                    "attribution": attribution,
                },
                ensure_ascii=False,
            ),
        )
    state = _read_state(runs_dir, spec.id)
    state.update(
        {
            "last_run_at": ran_at,
            "last_attempt_at": ran_at,
            "last_status": "partial" if errors else "ok",
            "last_alerts": len(alerts),
            "metrics_sha256": metrics_sha256,
        }
    )
    state.pop("next_retry_at", None)
    if attribution:
        state["last_alert_headline"] = attribution["headline"]
    else:
        state.pop("last_alert_headline", None)
    if errors:
        state["last_errors"] = errors
    else:
        state.pop("last_errors", None)
    _write_state(runs_dir, spec.id, state)

    notification = {
        "task": {"id": spec.id, "name": spec.name},
        "ran_at": ran_at,
        "alerts": alerts,
        "attribution": attribution,
    }
    webhook_error: str | None = None
    webhook_status: int | None = None
    webhook_duration_ms: int | None = None
    email_error: str | None = None
    email_status: int | None = None
    sms_error: str | None = None
    sms_status: int | None = None
    notify_now = bool(alerts and notify)
    if notify_now and spec.webhook_url:
        webhook_error, webhook_status, webhook_duration_ms = _notify_result(
            spec.webhook_url, notification
        )
    if notify_now and spec.email_to:
        subject = f"[data-agent] {spec.name} 触发 {len(alerts)} 项告警"
        email_error, email_status, _ = _send_email_result(
            spec.email_to, subject, _alert_text(notification)
        )
    if notify_now and spec.sms_to:
        sms_error, sms_status, _ = _send_sms_result(spec.sms_to, _alert_text(notification))
    result["notified"] = bool(notify_now and spec.webhook_url and not webhook_error)
    result["webhook_error"] = webhook_error
    result["webhook_status"] = webhook_status
    result["webhook_duration_ms"] = webhook_duration_ms
    result["email_sent"] = bool(notify_now and spec.email_to and not email_error)
    result["email_error"] = email_error
    result["email_status"] = email_status
    result["sms_sent"] = bool(notify_now and spec.sms_to and not sms_error)
    result["sms_error"] = sms_error
    result["sms_status"] = sms_status
    if webhook_error:
        state["last_webhook_error"] = webhook_error
    else:
        state.pop("last_webhook_error", None)
    if webhook_duration_ms is not None:
        # 只有真的尝试过推送才落这两个字段：区分「没配 webhook」与「推失败」。
        state["last_webhook_status"] = webhook_status
        state["last_webhook_duration_ms"] = webhook_duration_ms
    for channel, error, status in (
        ("email", email_error, email_status),
        ("sms", sms_error, sms_status),
    ):
        if error:
            state[f"last_{channel}_error"] = error
        else:
            state.pop(f"last_{channel}_error", None)
        if status is not None:
            state[f"last_{channel}_status"] = status
    state["last_notified"] = result["notified"]
    _write_state(runs_dir, spec.id, state)
    return result


def _record_failure(runs_dir: str | Path, task_id: str, error: str, now: datetime) -> None:
    state = _read_state(runs_dir, task_id)
    state.update(
        {
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_status": "error",
            "last_error": error[:500],
            "next_retry_at": (now + timedelta(minutes=RETRY_BACKOFF_MINUTES)).isoformat(
                timespec="seconds"
            ),
        }
    )
    _write_state(runs_dir, task_id, state)


def run_due(
    runs_dir: str | Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    notify: bool = True,
) -> list[dict[str, Any]]:
    """运行所有到期任务；单任务失败只影响自己，错误写进结果与状态。"""
    moment = now or datetime.now().astimezone()
    results: list[dict[str, Any]] = []
    for spec in load_specs(runs_dir):
        try:
            state = _read_state(runs_dir, spec.id)
            if not is_due(
                spec,
                str(state.get("last_run_at") or ""),
                moment,
                next_retry_at=str(state.get("next_retry_at") or ""),
            ):
                continue
            results.append(run_automation(spec, runs_dir, dry_run=dry_run, notify=notify))
        except Exception as exc:  # noqa: BLE001 — 单任务失败不阻断批次
            message = str(exc)[:500]
            if not dry_run:
                _record_failure(runs_dir, spec.id, message, moment)
            results.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "ran_at": moment.isoformat(timespec="seconds"),
                    "error": message,
                }
            )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _serve_loop(args: argparse.Namespace, runs_dir: Path) -> None:  # pragma: no cover
    """前台轮询：按 --poll-seconds 周期跑到期任务，Ctrl+C 退出。"""
    while True:
        results = run_due(runs_dir, notify=not args.no_notify)
        if results:
            sys.stdout.write(json.dumps(results, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        time.sleep(max(1, args.poll_seconds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data-agent-automation", description="定时分析任务")
    parser.add_argument(
        "--runs-dir",
        default=str(Path(os.getenv("DATA_AGENT_RUNS_DIR", "runs"))),
        help="运行根目录（默认为 DATA_AGENT_RUNS_DIR 或 ./runs；必须写在子命令之前）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="新增/更新一条任务（--spec 指向 JSON 文件）")
    add.add_argument("--spec", required=True, help="任务定义 JSON 文件路径")

    sub.add_parser("list", help="列出全部任务")
    status = sub.add_parser("status", help="汇总任务运行状态（巡检/看板用）")
    status.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="有到期任务或上次运行失败时以退出码 1 结束（供监控调用）",
    )
    dashboard = sub.add_parser("dashboard", help="生成零依赖的静态 HTML 看板")
    dashboard.add_argument("--out", help="输出 HTML 路径（省略则打印到 stdout）")
    dashboard.add_argument(
        "--refresh-seconds", type=int, default=0, help="meta refresh 间隔（0=不自动刷新）"
    )

    remove = sub.add_parser("remove", help="删除任务")
    remove.add_argument("--id", required=True)

    run = sub.add_parser("run", help="立即运行指定任务")
    run.add_argument("--id", required=True)
    run.add_argument("--dry-run", action="store_true", help="只计算与检测，不写历史/告警、不推送")
    run.add_argument("--no-notify", action="store_true")

    due = sub.add_parser("run-due", help="运行所有到期任务（供 cron / 任务计划调用）")
    due.add_argument("--dry-run", action="store_true")
    due.add_argument("--no-notify", action="store_true", help="不发送 webhook")

    loop = sub.add_parser("loop", help="前台轮询模式（Ctrl+C 退出）")
    loop.add_argument("--poll-seconds", type=int, default=60)
    loop.add_argument("--no-notify", action="store_true")

    args = parser.parse_args(argv)
    runs_dir = Path(args.runs_dir)
    try:
        if args.command == "add":
            payload = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "automations" in payload:
                specs = [parse_spec(item) for item in payload["automations"]]
                for spec in specs:
                    add_spec(runs_dir, spec)
                output: Any = {"added": [spec.id for spec in specs]}
            else:
                spec = parse_spec(payload)
                add_spec(runs_dir, spec)
                output = {"added": [spec.id]}
        elif args.command == "list":
            output = [spec.to_dict() for spec in load_specs(runs_dir)]
        elif args.command == "status":
            output = status_overview(runs_dir)
        elif args.command == "dashboard":
            overview = status_overview(runs_dir)
            html_text = render_dashboard(
                overview, refresh_seconds=max(0, int(args.refresh_seconds or 0))
            )
            if not args.out:
                sys.stdout.write(html_text)
                return 0
            target = Path(args.out).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, html_text)
            output = {"path": str(target), "summary": overview["summary"]}
        elif args.command == "remove":
            output = {"removed": remove_spec(runs_dir, args.id)}
        elif args.command == "run":
            specs = {spec.id: spec for spec in load_specs(runs_dir)}
            if args.id not in specs:
                raise ValueError(f"任务不存在：{args.id}")
            output = run_automation(
                specs[args.id],
                runs_dir,
                dry_run=args.dry_run,
                notify=not args.no_notify,
            )
        elif args.command == "run-due":
            output = run_due(runs_dir, dry_run=args.dry_run, notify=not args.no_notify)
        else:
            _serve_loop(args, runs_dir)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"错误：{exc}\n")
        return 2
    sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    if args.command == "status" and args.fail_on_alert:
        summary = output["summary"]
        # 告警也算「需要人看一眼」：上次运行有告警但还没到下次调度时，
        # 监控不应因为 due=0 就显示一切正常。
        if summary["due"] or summary["errors"] or summary["alerts"]:
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
