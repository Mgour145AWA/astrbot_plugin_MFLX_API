"""AstrBot 插件：Minecraft 服务器面板远程控制。

通过面板左侧「API 管理」中开放的远程 API（/api/remote/*），在聊天里查询
服务器列表 / 运行状态 / 最近日志，并执行启动、停止、重启以及发送游戏内指令。

接口约定（依据面板 API 文档）：

    GET  /api/remote/servers                      服务器列表
    GET  /api/remote/status?server=<名称>          运行状态
    GET  /api/remote/log?server=<名称>&lines=100   最近日志
    POST /api/remote/start    {"server": "<名称>"}
    POST /api/remote/stop     {"server": "<名称>"}
    POST /api/remote/restart  {"server": "<名称>"}
    POST /api/remote/command  {"server": "<名称>", "command": "say 你好"}

鉴权：
    请求头 ``X-API-Key: <Key>``，或 URL 参数 ``?key=<Key>``。

错误码：
    401 Key 缺失或错误 / 403 未启用或不在白名单 / 404 服务器不存在
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot >= 3.4.15 才会传入 AstrBotConfig
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover - 兼容旧版本
    AstrBotConfig = dict

try:
    import httpx
except ImportError:  # pragma: no cover - 依赖缺失时给出可读提示而不是崩溃
    httpx = None  # type: ignore[assignment]


PLUGIN_NAME = "astrbot_plugin_mcpanel_remote"
PLUGIN_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 15

# 与 _conf_schema.json 中的默认值保持一致。
# 之所以在代码里再写一份，是为了兼容「用户配置文件里缺少该项」的情况
# （例如插件升级后新增了配置项、或配置文件被手工修改过）。
DEFAULT_COMMAND_ALLOWLIST = [
    "list", "tps", "time", "seed", "help", "say", "tell", "msg",
    "weather", "whitelist", "banlist",
]
DEFAULT_COMMAND_BLOCKLIST = [
    "stop", "restart", "reload", "op", "deop", "ban", "ban-ip", "pardon",
    "kick", "kill", "fill", "setblock", "execute", "datapack", "gamerule",
    "save-all", "save-off",
]

# HTTP 状态码 -> 中文排错提示
_HTTP_ERROR_HINT = {
    400: "请求参数有误，请检查服务器名是否填写正确。",
    401: "API Key 缺失或错误，请检查插件配置中的「API Key」。",
    403: "远程 API 未启用，或 AstrBot 所在机器的 IP 不在面板白名单中。",
    404: "服务器不存在，请先用 /mc list 查看可用的服务器名。",
    405: "请求方法不被支持，请确认面板版本与接口文档是否一致。",
    429: "请求过于频繁，请稍后再试。",
    500: "面板内部错误，请查看面板自身的日志。",
    502: "面板网关错误，服务可能正在重启。",
    503: "面板服务不可用，请确认面板进程是否正常运行。",
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _is_admin(event: AstrMessageEvent) -> bool:
    """判断消息发送者是否为管理员，兼容 AstrBot 各版本的事件属性差异。"""
    try:
        role = getattr(event, "role", None)
        if isinstance(role, str) and role.lower() in ("admin", "owner", "administrator"):
            return True

        flag = getattr(event, "is_admin", None)
        if callable(flag):
            return bool(flag())
        if isinstance(flag, bool):
            return flag
    except Exception as exc:  # pragma: no cover - 权限判断失败时按非管理员处理
        logger.debug(f"[{PLUGIN_NAME}] 权限判断失败：{exc}")
    return False


def _parse_args(message_str: str, cmd: str) -> list[str]:
    """从原始消息中取出指令之后的参数列表。

    同时兼容「唤醒前缀已被剥离」和「唤醒前缀仍在」两种情况，
    例如 ``/mc list``、``mc list``、``bot mc list``、``/mc_list``。
    """
    text = re.sub(r"\s+", " ", (message_str or "").strip())
    if not text:
        return []

    tokens = [tok for tok in text.split(" ") if tok]
    if not tokens:
        return []

    # 同时接受完整指令名与去掉前缀后的子名，例如 "mc_status" 也能识别 "/mc status ..."
    names = {cmd.lower()}
    if "_" in cmd:
        names.add(cmd.split("_", 1)[1].lower())

    # 唤醒前缀通常只出现在最前面，最多向前检查 3 个 token 即可（如 "bot mc list"）
    for index, token in enumerate(tokens[:3]):
        if token.lstrip("/!！").lower() in names:
            return tokens[index + 1 :]
    return tokens[1:]


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    try:
        value = config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list_str(value: Any) -> list[str]:
    if isinstance(value, str):
        # 兼容用户在配置里用逗号 / 换行分隔的写法
        return [part.strip() for part in re.split(r"[,\n;]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _clip(text: str, limit: int, keep_tail: bool = False) -> tuple[str, bool]:
    """按字符数裁剪文本，返回 (文本, 是否被裁剪)。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    if keep_tail:
        return "…（前文已省略）\n" + text[-limit:], True
    return text[:limit] + "\n…（后文已省略）", True


def _pretty(data: Any, limit: int = 1200) -> str:
    """把任意响应体渲染成可读文本。"""
    if data is None:
        return "（面板未返回内容）"
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return "（面板未返回内容）"
        clipped, _ = _clip(text, limit)
        return clipped
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(data)
    clipped, _ = _clip(text, limit)
    return clipped


def _find_first_list(data: Any, depth: int = 0) -> list | None:
    """在嵌套响应中寻找第一个列表（服务器列表的返回结构在不同面板版本可能不同）。"""
    if depth > 4:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("servers", "server_list", "list", "items", "data", "result"):
            if key in data:
                found = _find_first_list(data[key], depth + 1)
                if found is not None:
                    return found
        for value in data.values():
            found = _find_first_list(value, depth + 1)
            if found is not None:
                return found
    return None


def _find_first_dict(data: Any, depth: int = 0) -> dict | None:
    """在嵌套响应中寻找最内层的信息字典。"""
    if depth > 4:
        return None
    if isinstance(data, dict):
        for key in ("data", "result", "status", "info", "server"):
            inner = data.get(key)
            if isinstance(inner, dict):
                found = _find_first_dict(inner, depth + 1)
                if found is not None:
                    return found
        return data
    return None


_SERVER_NAME_KEYS = ("name", "server", "server_name", "serverName", "id", "label", "title")
_PAYLOAD_NOISE_KEYS = {
    "code", "status", "success", "msg", "message", "error", "total", "count",
    "time", "timestamp", "data", "result", "servers", "list", "items", "page", "size",
}


def _server_name_of(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in _SERVER_NAME_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in item.values():
            if isinstance(value, dict):
                name = _server_name_of(value)
                if name:
                    return name
    return ""


def _server_state_of(item: Any) -> str:
    """尽量从列表项里解析出运行状态，用于列表展示。"""
    if not isinstance(item, dict):
        return ""
    for key in ("status", "state", "running", "online", "started"):
        if key in item:
            return _translate_status(item[key])
    for value in item.values():
        if isinstance(value, dict):
            state = _server_state_of(value)
            if state:
                return state
    return ""


_STATUS_LABELS = {
    "status": "状态", "state": "状态", "online": "在线", "running": "运行中",
    "started": "已启动", "players": "在线玩家", "player_count": "在线玩家",
    "online_players": "在线玩家", "playercount": "在线玩家", "player_list": "玩家列表",
    "max_players": "最大玩家数", "maxplayers": "最大玩家数", "slots": "最大玩家数",
    "max": "最大玩家数", "version": "游戏版本", "mc_version": "游戏版本",
    "type": "服务端类型", "server_type": "服务端类型", "core": "核心",
    "uptime": "运行时长", "started_at": "启动时间", "start_time": "启动时间",
    "cpu": "CPU 占用", "cpu_usage": "CPU 占用", "cpu_percent": "CPU 占用",
    "memory": "内存占用", "mem": "内存占用", "ram": "内存占用", "memory_usage": "内存占用",
    "tps": "TPS", "mspt": "MSPT",
    "port": "端口", "host": "主机", "ip": "IP", "address": "地址",
    "name": "服务器名", "server": "服务器名", "id": "ID",
    "timestamp": "时间", "time": "时间", "pid": "进程号",
}

_STATUS_TEXT = {
    "running": "运行中", "started": "运行中", "online": "运行中", "up": "运行中",
    "stopped": "已停止", "offline": "已停止", "down": "已停止", "exited": "已退出",
    "starting": "启动中", "stopping": "停止中", "restarting": "重启中",
    "unknown": "未知", "error": "异常", "crashed": "已崩溃",
}

_STATE_KEYS = {"status", "state", "online", "running", "started", "active", "enabled"}


def _translate_status(value: Any) -> str:
    if isinstance(value, bool):
        return "运行中" if value else "已停止"
    if isinstance(value, str):
        return _STATUS_TEXT.get(value.strip().lower(), value.strip())
    if value is None:
        return "未知"
    return str(value)


def _fmt_value(key: str, value: Any) -> str:
    lowered = key.lower()
    if lowered in _STATE_KEYS:
        return _translate_status(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "未知"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        if not value:
            return "无"
        if len(value) <= 12 and all(not isinstance(v, (dict, list)) for v in value):
            return "、".join(str(v) for v in value)
        return f"共 {len(value)} 项"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_servers(data: Any) -> str:
    names: list[str] = []

    found = _find_first_list(data)
    if found is not None:
        for item in found:
            name = _server_name_of(item)
            if name:
                state = _server_state_of(item)
                names.append(f"{name}  [{state}]" if state else name)

    if not names and isinstance(data, dict):
        # 兼容 {"ServerA": {...}, "ServerB": {...}} 这种以服务器名为 key 的返回
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(inner, dict):
            for key, value in inner.items():
                if str(key).lower() in _PAYLOAD_NOISE_KEYS or str(key).startswith("_"):
                    continue
                if isinstance(value, (dict, str)):
                    state = _server_state_of(value)
                    names.append(f"{key}  [{state}]" if state else str(key))

    if not names:
        return "面板未返回可识别的服务器列表。\n\n原始返回：\n" + _pretty(data)

    lines = [f"共 {len(names)} 个服务器："]
    for index, name in enumerate(names, start=1):
        lines.append(f"{index:>2}. {name}")
    return "\n".join(lines)


def _render_status(server: str, data: Any) -> str:
    info = _find_first_dict(data)
    if not isinstance(info, dict) or not info:
        return f"【{server}】运行状态\n{_pretty(data)}"

    lines = [f"【{server}】运行状态"]
    for key, value in info.items():
        if str(key).startswith("_"):
            continue
        label = _STATUS_LABELS.get(str(key).lower(), str(key))
        lines.append(f"· {label}：{_fmt_value(str(key), value)}")
    if len(lines) == 1:
        return f"【{server}】运行状态\n{_pretty(data)}"
    return "\n".join(lines)


_LOG_KEYS = ("log", "logs", "content", "text", "output", "lines", "data", "result")


def _extract_log_text(data: Any, depth: int = 0) -> str:
    if data is None or depth > 4:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(str(item) for item in data)
    if isinstance(data, dict):
        for key in _LOG_KEYS:
            if key in data:
                text = _extract_log_text(data[key], depth + 1)
                if text.strip():
                    return text
        return _pretty(data)
    return str(data)


def _render_log(server: str, data: Any, lines: int, max_chars: int) -> str:
    text = _extract_log_text(data).strip()
    if not text:
        return f"【{server}】最近日志\n面板未返回日志内容（服务器可能未运行，或日志文件为空）。"

    body, clipped = _clip(text, max(200, max_chars), keep_tail=True)
    header = f"【{server}】最近 {lines} 行日志"
    if clipped:
        header += "（内容较长，仅显示结尾部分）"
    return f"{header}\n{'-' * 20}\n{body}"


def _brief(data: Any) -> str:
    """把操作类接口的返回压缩成一行结论。"""
    if isinstance(data, str):
        text = data.strip()
        return text[:300] if text else "面板未返回额外信息。"
    if isinstance(data, dict):
        for key in ("message", "msg", "detail", "info"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _pretty(data, limit=400)


def _fail(error: str) -> str:
    return f"[失败] {error}"


# --------------------------------------------------------------------------- #
# 面板 API 客户端
# --------------------------------------------------------------------------- #
class PanelClient:
    """封装面板 /api/remote/* 接口的异步客户端。"""

    def __init__(self, config: Any):
        self._config = config
        self._client: Any = None

    # ---------------- 配置 ----------------
    @property
    def base_url(self) -> str:
        raw = str(_cfg_get(self._config, "base_url", "") or "").strip().rstrip("/")
        if not raw:
            return ""
        if not re.match(r"^https?://", raw, re.IGNORECASE):
            raw = "http://" + raw
        if not raw.lower().endswith("/api/remote"):
            raw = raw + "/api/remote"
        return raw

    @property
    def api_key(self) -> str:
        return str(_cfg_get(self._config, "api_key", "") or "").strip()

    @property
    def auth_mode(self) -> str:
        mode = str(_cfg_get(self._config, "auth_mode", "header") or "header").strip().lower()
        return mode if mode in ("header", "query") else "header"

    @property
    def timeout(self) -> float:
        return float(max(3, _as_int(_cfg_get(self._config, "timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)))

    # ---------------- 生命周期 ----------------
    async def _get_client(self) -> Any:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 10.0)),
                verify=_as_bool(_cfg_get(self._config, "verify_ssl", True), True),
                trust_env=_as_bool(_cfg_get(self._config, "use_system_proxy", False), False),
                headers={"User-Agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as exc:  # pragma: no cover
                logger.debug(f"[{PLUGIN_NAME}] 关闭 HTTP 客户端失败：{exc}")
        self._client = None

    # ---------------- 底层请求 ----------------
    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[bool, Any, str]:
        """发起一次请求，返回 (是否成功, 解析后的响应, 错误信息)。"""
        if httpx is None:
            return False, None, "缺少 httpx 依赖，请在插件管理页重新安装依赖后重载插件。"

        base = self.base_url
        if not base:
            return False, None, "尚未配置「面板地址」，请在插件配置中填写，例如 http://192.168.1.10:8570"

        if not self.api_key:
            return False, None, "尚未配置「API Key」，请在插件配置中填写（面板「API 管理」页可获取）。"

        url = f"{base}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        query = dict(params or {})
        if self.auth_mode == "query":
            query["key"] = self.api_key
        else:
            headers["X-API-Key"] = self.api_key

        client = await self._get_client()
        try:
            response = await client.request(
                method.upper(), url, params=query or None, json=json_body, headers=headers
            )
        except httpx.TimeoutException:
            return False, None, (
                f"请求超时（{self.timeout:.0f} 秒）。启动或停止服务器可能耗时较久，"
                "可在插件配置中调大「请求超时」。"
            )
        except httpx.ConnectError as exc:
            return False, None, (
                f"无法连接面板 {base}。请检查地址、端口是否正确，以及 AstrBot 与面板是否互通。"
                f"（{exc}）"
            )
        except httpx.HTTPError as exc:
            return False, None, f"请求面板失败：{exc}"

        raw_text = response.text or ""
        payload: Any = None
        if raw_text:
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                payload = None

        if response.is_success:
            return True, (payload if payload is not None else raw_text), ""

        hint = _HTTP_ERROR_HINT.get(response.status_code, "")
        detail = ""
        if isinstance(payload, dict):
            for key in ("message", "msg", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
                    break
        if not detail and raw_text.strip():
            detail = raw_text.strip()[:200]

        message = f"面板返回 HTTP {response.status_code}"
        if hint:
            message += f"：{hint}"
        if detail and detail not in hint:
            message += f"\n面板消息：{detail}"
        return False, payload, message

    # ---------------- 业务接口 ----------------
    async def list_servers(self) -> tuple[bool, Any, str]:
        return await self.request("GET", "servers")

    async def status(self, server: str) -> tuple[bool, Any, str]:
        return await self.request("GET", "status", params={"server": server})

    async def log(self, server: str, lines: int) -> tuple[bool, Any, str]:
        return await self.request("GET", "log", params={"server": server, "lines": lines})

    async def start(self, server: str) -> tuple[bool, Any, str]:
        return await self.request("POST", "start", json_body={"server": server})

    async def stop(self, server: str) -> tuple[bool, Any, str]:
        return await self.request("POST", "stop", json_body={"server": server})

    async def restart(self, server: str) -> tuple[bool, Any, str]:
        return await self.request("POST", "restart", json_body={"server": server})

    async def command(self, server: str, command: str) -> tuple[bool, Any, str]:
        return await self.request(
            "POST", "command", json_body={"server": server, "command": command}
        )


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
_USAGE_TOKENS = {
    "list": ("list", "ls", "列表", "服务器", "服务器列表"),
    "status": ("status", "state", "状态", "信息", "info"),
    "log": ("log", "logs", "日志", "最近日志"),
    "start": ("start", "启动", "开服", "开机"),
    "stop": ("stop", "停止", "关服", "关机"),
    "restart": ("restart", "重启", "重开", "reboot"),
    "command": ("cmd", "command", "指令", "执行", "exec"),
    "help": ("help", "h", "?", "帮助", "菜单"),
}


def _resolve_action(token: str) -> str | None:
    token = (token or "").strip().lower()
    if not token:
        return None
    for action, tokens in _USAGE_TOKENS.items():
        if token in tokens:
            return action
    return None


@register(
    PLUGIN_NAME,
    "Mgour145AWA",
    "接入 Minecraft 服务器面板的远程 API，在聊天中查看服务器列表、运行状态与日志，"
    "并执行启动 / 停止 / 重启与游戏内指令。",
    PLUGIN_VERSION,
    "",
)
class MCPanelRemotePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self.client = PanelClient(self.config)
        logger.info(
            f"[{PLUGIN_NAME}] v{PLUGIN_VERSION} 已加载，"
            f"面板地址：{self.client.base_url or '（未配置）'}"
        )

    # ---------------- 便利读取 ----------------
    def _get(self, key: str, default: Any = None) -> Any:
        return _cfg_get(self.config, key, default)

    def _help_text(self) -> str:
        base = self.client.base_url
        lines = [
            "Minecraft 服务器控制台",
            "",
            "/mc list                     查看服务器列表",
            "/mc status <服务器名>         查看运行状态",
            "/mc log <服务器名> [行数]     查看最近日志",
            "/mc start <服务器名>          启动服务器",
            "/mc stop <服务器名>           停止服务器",
            "/mc restart <服务器名>        重启服务器",
            "/mc cmd <服务器名> <指令>     发送游戏内指令",
            "/mc help                     显示本帮助",
            "",
            "简写形式：/mc_list、/mc_status、/mc_log、/mc_start、/mc_stop、/mc_restart、/mc_cmd",
        ]
        if not base:
            lines += ["", "[提示] 尚未配置面板地址，请先在插件配置中填写「面板地址」与「API Key」。"]
        return "\n".join(lines)

    def _write_denied(self, event: AstrMessageEvent) -> str | None:
        if _as_bool(self._get("admin_only_write", True), True) and not _is_admin(event):
            return "[失败] 该操作会改变服务器运行状态，仅限管理员使用。"
        return None

    def _read_denied(self, event: AstrMessageEvent) -> str | None:
        if _as_bool(self._get("admin_only_read", False), False) and not _is_admin(event):
            return "[失败] 查询类指令当前仅限管理员使用。"
        return None

    def _validate_game_command(self, raw: str) -> tuple[str, str | None]:
        """校验游戏内指令，返回 (清洗后的指令, 错误信息)。"""
        command = raw.strip()
        while command.startswith("/"):
            command = command[1:].strip()

        if not command:
            return "", "指令内容不能为空。"

        max_length = _as_int(self._get("max_command_length", 256), 256)
        if len(command) > max_length:
            return "", f"指令过长，上限 {max_length} 个字符。"

        if any(ch in command for ch in ("\n", "\r", "\t")):
            return "", "指令中不允许包含换行符或制表符。"

        base = command.split(" ", 1)[0].lower().lstrip("/")

        blocklist = {
            item.lstrip("/").lower()
            for item in _as_list_str(self._get("command_blocklist", DEFAULT_COMMAND_BLOCKLIST))
        }
        if base in blocklist:
            return "", f"指令「{base}」在禁用列表中（可通过插件配置的「指令黑名单」调整）。"

        if not _as_bool(self._get("allow_any_command", False), False):
            allowlist = {
                item.lstrip("/").lower()
                for item in _as_list_str(self._get("command_allowlist", DEFAULT_COMMAND_ALLOWLIST))
            }
            if base not in allowlist:
                return "", (
                    f"指令「{base}」不在白名单中。\n"
                    "如需放开限制，请在插件配置中开启「允许执行任意指令」，"
                    "或把该指令加入「指令白名单」。"
                )

        return command, None

    # ---------------- 核心执行逻辑 ----------------
    async def _run(self, event: AstrMessageEvent, action_token: str, rest: list[str]):
        """执行一个子命令，逐个 yield 需要发送的文本。"""
        action = _resolve_action(action_token)

        if action is None:
            if action_token:
                yield f"未知子命令：{action_token}\n\n" + self._help_text()
            else:
                yield self._help_text()
            return

        if action == "help":
            yield self._help_text()
            return

        # ---- 只读操作 ----
        if action == "list":
            denied = self._read_denied(event)
            if denied:
                yield denied
                return
            ok, data, error = await self.client.list_servers()
            yield _render_servers(data) if ok else _fail(error)
            return

        if action == "status":
            denied = self._read_denied(event)
            if denied:
                yield denied
                return
            if not rest:
                yield "用法：/mc status <服务器名>"
                return
            server = rest[0]
            ok, data, error = await self.client.status(server)
            yield _render_status(server, data) if ok else _fail(error)
            return

        if action == "log":
            denied = self._read_denied(event)
            if denied:
                yield denied
                return
            if not rest:
                yield "用法：/mc log <服务器名> [行数]\n例如：/mc log ServerA 200"
                return
            server = rest[0]
            lines = _as_int(self._get("default_log_lines", 100), 100)
            if len(rest) > 1:
                parsed = None
                try:
                    parsed = int(rest[1])
                except (TypeError, ValueError):
                    parsed = None
                if parsed is None:
                    yield f"行数必须是整数，收到：{rest[1]}"
                    return
                lines = parsed
            max_lines = max(1, _as_int(self._get("max_log_lines", 500), 500))
            lines = max(1, min(lines, max_lines))

            ok, data, error = await self.client.log(server, lines)
            if not ok:
                yield _fail(error)
                return
            max_chars = max(200, _as_int(self._get("max_reply_chars", 1800), 1800))
            yield _render_log(server, data, lines, max_chars)
            return

        # ---- 写操作 ----
        if action in ("start", "stop", "restart"):
            if not rest:
                yield f"用法：/mc {action} <服务器名>"
                return
            server = rest[0]
            denied = self._write_denied(event)
            if denied:
                yield denied
                return

            action_cn = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
            yield f"正在{action_cn}服务器「{server}」，请稍候…"

            ok, data, error = await getattr(self.client, action)(server)
            if ok:
                yield f"[成功] 已提交{action_cn}操作：{server}\n{_brief(data)}"
            else:
                yield f"[失败] {action_cn}「{server}」失败：\n{error}"
            return

        if action == "command":
            if len(rest) < 2:
                yield "用法：/mc cmd <服务器名> <指令>\n例如：/mc cmd ServerA say 你好"
                return
            server = rest[0]
            denied = self._write_denied(event)
            if denied:
                yield denied
                return

            command, error = self._validate_game_command(" ".join(rest[1:]))
            if error:
                yield f"[失败] {error}"
                return

            ok, data, error = await self.client.command(server, command)
            if ok:
                yield f"[成功] 已在「{server}」执行：{command}\n{_brief(data)}"
            else:
                yield f"[失败] 在「{server}」执行指令失败：\n{error}"
            return

        yield self._help_text()

    async def _emit(self, event: AstrMessageEvent, cmd: str, forced_action: str | None = None):
        """指令入口：解析参数后把 _run 的输出逐条发出。"""
        args = _parse_args(event.message_str, cmd)
        if forced_action is None:
            token = args[0] if args else ""
            rest = args[1:]
        else:
            token = forced_action
            rest = args

        async for text in self._run(event, token, rest):
            yield event.plain_result(text)

    # ---------------- 指令 ----------------
    @filter.command("mc")
    async def mc(self, event: AstrMessageEvent):
        """Minecraft 服务器控制台。用法：/mc <子命令> [参数]"""
        async for result in self._emit(event, "mc"):
            yield result

    @filter.command("mc_help")
    async def mc_help(self, event: AstrMessageEvent):
        """显示 MC 服务器控制台帮助"""
        async for result in self._emit(event, "mc_help", "help"):
            yield result

    @filter.command("mc_list")
    async def mc_list(self, event: AstrMessageEvent):
        """查看面板上的服务器列表"""
        async for result in self._emit(event, "mc_list", "list"):
            yield result

    @filter.command("mc_status")
    async def mc_status(self, event: AstrMessageEvent):
        """查看指定服务器的运行状态。用法：/mc_status <服务器名>"""
        async for result in self._emit(event, "mc_status", "status"):
            yield result

    @filter.command("mc_log")
    async def mc_log(self, event: AstrMessageEvent):
        """查看指定服务器的最近日志。用法：/mc_log <服务器名> [行数]"""
        async for result in self._emit(event, "mc_log", "log"):
            yield result

    @filter.command("mc_start")
    async def mc_start(self, event: AstrMessageEvent):
        """启动指定服务器。用法：/mc_start <服务器名>"""
        async for result in self._emit(event, "mc_start", "start"):
            yield result

    @filter.command("mc_stop")
    async def mc_stop(self, event: AstrMessageEvent):
        """停止指定服务器。用法：/mc_stop <服务器名>"""
        async for result in self._emit(event, "mc_stop", "stop"):
            yield result

    @filter.command("mc_restart")
    async def mc_restart(self, event: AstrMessageEvent):
        """重启指定服务器。用法：/mc_restart <服务器名>"""
        async for result in self._emit(event, "mc_restart", "restart"):
            yield result

    @filter.command("mc_cmd")
    async def mc_cmd(self, event: AstrMessageEvent):
        """向指定服务器发送游戏内指令。用法：/mc_cmd <服务器名> <指令>"""
        async for result in self._emit(event, "mc_cmd", "command"):
            yield result

    # ---------------- LLM 工具 ----------------
    @filter.llm_tool(name="mc_panel_ops")
    async def mc_panel_ops(
        self,
        event: AstrMessageEvent,
        action: str = "",
        server: str = "",
        lines: int = 100,
        command: str = "",
    ):
        """远程操作 Minecraft 服务器面板：查询服务器列表、运行状态、最近日志，或启动、停止、重启服务器、发送游戏内指令。

        Args:
            action(string): 要执行的操作，只能是 list（服务器列表）、status（运行状态）、log（最近日志）、start（启动）、stop（停止）、restart（重启）、command（发送游戏内指令）之一
            server(string): 目标服务器名称。action 为 list 时可留空，其它操作必须提供
            lines(number): 当 action 为 log 时返回的日志行数，默认 100
            command(string): 当 action 为 command 时要发送的游戏内指令，例如 "say 你好"
        """
        if httpx is None:
            return "插件依赖 httpx 缺失，请先在插件管理页重新安装依赖。"

        if not _as_bool(self._get("enable_llm_tool", True), True):
            return "管理员已关闭本插件的 LLM 工具能力，请改用 /mc 指令。"

        token = str(action or "").strip().lower()
        server_name = str(server or "").strip()
        rest: list[str]

        if token in ("list", "ls"):
            token, rest = "list", []
        elif token in ("status", "state"):
            if not server_name:
                return "action=status 需要同时提供 server 参数。"
            token, rest = "status", [server_name]
        elif token in ("log", "logs"):
            if not server_name:
                return "action=log 需要同时提供 server 参数。"
            token, rest = "log", [server_name, str(_as_int(lines, 100))]
        elif token in ("start", "stop", "restart"):
            if not server_name:
                return f"action={token} 需要同时提供 server 参数。"
            rest = [server_name]
        elif token in ("command", "cmd", "exec"):
            game_command = str(command or "").strip()
            if not server_name or not game_command:
                return "action=command 需要同时提供 server 与 command 参数。"
            token, rest = "command", [server_name, game_command]
        elif not token:
            return "缺少 action 参数。可选值：list、status、log、start、stop、restart、command。"
        else:
            return f"不支持的 action：{action}。可选值：list、status、log、start、stop、restart、command。"

        parts = [text async for text in self._run(event, token, rest)]
        return "\n".join(parts) if parts else "操作未产生任何结果。"

    async def terminate(self):
        """插件被卸载 / 停用时释放连接资源。"""
        await self.client.close()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，HTTP 连接已释放。")
