# astrbot_plugin_mcpanel_remote

> 把 Minecraft 服务器面板「API 管理」里开放的远程 API 接进 AstrBot —— 在 QQ / 群聊里查状态、看日志、一键启停服务器。

## 一、功能

| 能力 | 说明 |
| --- | --- |
| 服务器列表 | `/mc list`，列出面板上所有服务器及其运行状态 |
| 运行状态 | `/mc status <服务器名>`，展示状态、玩家数、版本、内存、TPS 等信息 |
| 最近日志 | `/mc log <服务器名> [行数]`，自动保留日志结尾部分并做长度截断 |
| 启停与重启 | `/mc start <服务器名`、`/mc stop <服务器名`、`/mc restart <服务器名` |
| 游戏内指令 | `/mc cmd <服务器名> <指令>`，可在群里 `say` 公告、查询 `tps` 等 |
| LLM 工具 | 注册了 `mc_panel_ops` 工具，AI 可直接代你操作（如「看下 ServerA 的日志」） |

安全设计：

- 写操作（启停 / 重启 / 发指令）默认**仅管理员可用**；
- 游戏内指令默认走**白名单**，且始终受**黑名单**拦截；
- 自动过滤换行符并限制指令长度，避免一次提交多条指令；
- API Key 在 WebUI 中以密码框显示（`secret: true`），不会被插件回显或写入日志。

## 二、接口对照

本插件严格按面板 API 文档实现：

| 方法 | 路径 | 插件用途 |
| --- | --- | --- |
| GET | `/api/remote/servers` | 服务器列表 |
| GET | `/api/remote/status?server=<名称>` | 运行状态 |
| GET | `/api/remote/log?server=<名称>&lines=100` | 最近日志 |
| POST | `/api/remote/start` `{"server":"<名称>"}` | 启动 |
| POST | `/api/remote/stop` `{"server":"<名称>"}` | 停止 |
| POST | `/api/remote/restart` `{"server":"<名称>"}` | 重启 |
| POST | `/api/remote/command` `{"server":"<名称>","command":"say 你好"}` | 执行指令 |

鉴权支持两种方式，在插件配置中切换：

- `header`：请求头 `X-API-Key: <你的Key>`（默认，推荐）
- `query`：URL 参数 `?key=<你的Key>`

错误码会翻译成中文提示：`401` Key 缺失或错误、`403` 未启用或不在白名单、`404` 服务器不存在。

## 三、安装

1. 下载 `astrbot_plugin_mcpanel_remote` 目录（或 `astrbot_plugin_mcpanel_remote.zip`）。
2. 放到 AstrBot 的插件目录下：

   ```text
   AstrBot/
   └── data/
       └── plugins/
           └── astrbot_plugin_mcpanel_remote/
               ├── main.py
               ├── metadata.yaml
               ├── _conf_schema.json
               ├── requirements.txt
               ├── logo.png
               └── README.md
   ```

3. 在 AstrBot WebUI「插件」页点击**重载插件**（或重启 AstrBot）。依赖 `httpx` 会随插件自动安装；若提示模块缺失，在插件卡片上点一次「安装依赖」再重载。
4. 打开插件配置，填写**面板地址**与 **API Key**，保存。

> 也可以通过 WebUI 的「安装插件 → 上传 ZIP」直接选择本目录打包出的 zip。

## 四、配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 面板地址 `base_url` | `http://127.0.0.1:8570` | 只填到端口即可，插件自动补 `/api/remote` |
| API Key `api_key` | 空 | 面板「API 管理」中获取 |
| 鉴权方式 `auth_mode` | `header` | `header` 或 `query` |
| 请求超时 `timeout` | 15 | 秒。启停服务器慢的话调到 30+ |
| 校验 HTTPS 证书 `verify_ssl` | 开 | 自签证书面板可关闭 |
| 使用系统代理 `use_system_proxy` | 关 | 内网面板建议保持关闭 |
| 默认日志行数 `default_log_lines` | 100 | `/mc log` 未指定行数时使用 |
| 日志行数上限 `max_log_lines` | 500 | 硬上限 |
| 单条回复最大字符数 `max_reply_chars` | 1800 | 超出则保留结尾并提示省略 |
| 查询类指令仅限管理员 `admin_only_read` | 关 | 公开群建议开启 |
| 管理类指令仅限管理员 `admin_only_write` | 开 | 强烈建议保持开启 |
| 允许执行任意指令 `allow_any_command` | 关 | 关闭时只允许白名单指令 |
| 指令白名单 `command_allowlist` | `list/tps/time/say/...` | 不带斜杠 |
| 指令黑名单 `command_blocklist` | `stop/reload/op/...` | 优先级高于白名单 |
| 游戏内指令最大长度 `max_command_length` | 256 | — |
| 启用 LLM 工具 `enable_llm_tool` | 开 | 允许 AI 直接调用面板接口 |

## 五、指令

```text
/mc list                     查看服务器列表
/mc status <服务器名>         查看运行状态
/mc log <服务器名> [行数]     查看最近日志
/mc start <服务器名>          启动服务器
/mc stop <服务器名>           停止服务器
/mc restart <服务器名>        重启服务器
/mc cmd <服务器名> <指令>     发送游戏内指令
/mc help                     显示帮助
```

同时提供简写指令：`/mc_list`、`/mc_status`、`/mc_log`、`/mc_start`、`/mc_stop`、`/mc_restart`、`/mc_cmd`、`/mc_help`。

子命令还支持中文写法，例如 `/mc 列表`、`/mc 状态 ServerA`、`/mc 重启 ServerA`。

### 使用示例

```text
群友：/mc list
机器人：共 2 个服务器：
          1. Survival  [运行中]
          2. Creative  [已停止]

群友：/mc status Survival
机器人：【Survival】运行状态
        · 状态：运行中
        · 玩家数：3 / 20
        · 版本：1.21.11
        · TPS：20.0

管理员：/mc resticon Survival
机器人：未知子命令：resticon
        （后跟完整帮助文本）

管理员：/mc restart Survival
机器人：正在重启服务器「Survival」，请稍候…
机器人：[成功] 已提交重启操作：Survival

管理员：/mc cmd Survival say 十分钟后维护
机器人：[成功] 已在「Survival」执行：say 十分钟后维护
```

## 六、常见问题

**Q：提示「无法连接面板」**
A：确认面板地址与端口（默认 8570）正确；AstrBot 与面板不在同一台机器时不能用 `127.0.0.1`；AstrBot 若跑在 Docker 里，`127.0.0.1` 指向容器自身，请改用宿主机局域网 IP（如 `http://192.168.1.10:8570`）。另外确认防火墙放行。

**Q：提示 401 / 403**
A：401 说明 Key 错误或没填（重新复制面板上的 Key）；403 说明面板侧没启用远程 API，或 AstrBot 的出口 IP 不在白名单内——注意在 Docker/NAT 环境下需要通过的是宿主机对外的 IP。

**Q：提示 404 服务器不存在**
A：服务器名要和面板里显示的完全一致（区分大小写、空格）。先用 `/mc list` 复制准确名称。

**Q：启动或停止时超时**
A：把「请求超时」调到 30~60 秒。插件会先回一条「正在启动，请稍候…」，成功后紧跟结果。

**Q：日志太长被截断**
A：调大「单条回复最大字符数」，或请求更少的行数。日志默认保留**结尾部分**，因为最新内容最有价值。

**Q：AI 说没有权限**
A：LLM 工具与指令共用同一套权限校验，写操作仍然要求调用者具备管理员身份，这是刻意设计。

## 七、开发者备注

- 插件兼容 AstrBot `>=4.5.0`，使用 `httpx` 异步客户端，连接复用，`terminate()` 时释放。
- 指令匹配同时兼容「唤醒前缀已剥离」与「前缀仍在」的两种情况，因此 `/mc list`、`mc list`、`bot mc list`、`/mc_list` 都能正确解析参数。
- 面板不同版本返回的 JSON 结构可能不同，插件对服务器列表 / 状态 / 日志做了**递归结构探测**，能适配 `[...]`、`{"servers":[...]}`、`{"data":{...}}` 等常见形态；解析失败时会原样输出 JSON 便于排查。
- 需要二次开发时，业务逻辑集中在 `PanelClient`（接口封装）与 `MCPanelRemotePlugin._run()`（指令分发）两处，便于扩展。

## 八、许可

MIT License。
