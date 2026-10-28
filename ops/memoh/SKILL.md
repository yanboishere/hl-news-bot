---
name: hl-news-bot-ops
description: 运维这个工作区里的 hl-news-bot（NewsLiquid 新闻评级 → Hyperliquid 永续合约，默认模拟盘）：查看状态、持仓和权益，启动、停止、重启，看日志，换 NewsLiquid token，改套餐，更新代码，排查保活。用户提到交易 bot、hl-news-bot、NewsLiquid、持仓、保活或这个 bot 的定时检查时使用。
---

# hl-news-bot 运维

代码和数据在 `{{ROOT}}`（在 /data 下，工作区重启不会丢）。所有操作都通过下面这个命令，后文简写成 `hlbot`：

```
{{HLBOT}}
```

不要直接运行 `python -m hlnews.run`，也不要自己 nohup、kill 进程。守护进程（supervisor）负责拉起 bot、卡死重启、日志轮转。自己另起的 bot 会因为实例锁直接退出（退出码 3），自己 kill 的进程也会被守护进程马上拉起来。

## 命令

| 要做的事 | 命令 |
|---|---|
| 一行健康状态，不在运行就拉起 | `hlbot ensure --quiet` |
| 详细状态：守护进程、心跳、权益、持仓、最近新闻、控制台、hooks | `hlbot status` |
| 日志（all / bot / app / sup / ui，`-n` 指定行数） | `hlbot logs -n 60` |
| 全面体检；`--net` 会调用一次 NewsLiquid（扣 1 次额度）、Hyperliquid 和 ipinfo | `hlbot doctor` |
| 启动 / 停止 / 重启 | `hlbot start` / `hlbot stop` / `hlbot restart` |
| 查看或修改 NewsLiquid 套餐对应的轮询设置 | `hlbot plan`，或 `hlbot plan pro` 后再 `hlbot restart` |
| 更新代码（有持仓时不会自动重启） | `hlbot update` |
| 重新安装 Memoh hooks / 本技能 | `hlbot hooks` / `hlbot skill` |

## 必须遵守

1. **默认 paper（模拟盘）。** 不要加 `--live`，不要把 config.yaml 的 `mode` 改成 `live`。只有用户在当前对话里明确要求实盘、你复述了风险（真钱、可能亏损、重启时会市价平仓、重启后不会接管交易所上已有的持仓）并且用户再次确认后，才可以这样做。实盘需要两把钥匙同时打开：`config.yaml` 里 `mode: live`，并且用 `hlbot restart --live` 启动。
2. **不读、不打印、不复制 `.env`** 的内容，也不碰任何 token 或私钥。要检查只看“有没有填”，`hlbot doctor` 会告诉你。用户要填或换 NewsLiquid token 时，请他在 Memoh 的 **Files** 标签页打开 `{{ROOT}}/.env`，把 `OPENNEWS_TOKEN=` 这一行补上并保存。守护进程几秒内会自动重启 bot 让它生效（有持仓时等平仓后再生效）。不要让用户把 token 发在聊天里；如果他已经发了，提醒他到 NewsLiquid 的 MCP 页面刷新 token（旧的会作废），再按上面的方法填新的。
3. `hlbot stop` 的意思是“停下来并且保持停止”，hooks 和定时任务都不会再把它拉起来，所以只在用户要求时使用。`stop` 和 `restart` 都会让 bot 先把所有持仓按市价平掉。
4. 有持仓时不要重启，除非用户同意。
5. 汇报用中文，简短。先一句结论（正常 / 已重新拉起 / 有问题），有问题时再附上 `hlbot logs` 里相关的几行。

## 定时检查

用户会建一个每小时的定时任务让你检查。每次运行 `hlbot ensure --quiet`，然后按输出的第一个词处理：

- `OK`：正常，回复“正常”即可。但如果这一行里有“刚（重新）启动”，说明工作区或守护进程刚被重启过（多半是会话开始时的 hook 刚把它拉起来），要告诉用户一句，并附上 `hlbot logs sup -n 20` 里能说明原因的几行。
- `STOPPED`：用户自己停的，不要去启动，回复“已手动停止”即可。
- `RESTARTED`：告诉用户 bot 停过、已经重新拉起，附上 `hlbot logs sup -n 20` 里能说明原因的几行。
- `DEGRADED` 或 `FAILED`：运行 `hlbot status` 和 `hlbot logs -n 60`，概括原因发给用户。不要改代码，不要切实盘。

## 保活是怎么做的（排查时看）

- **守护进程** `ops/memoh/supervisor.py`：
  - bot 退出就重启。连续很快崩溃时退避，最长等 5 分钟。
  - 心跳文件 `data/state.json` 每 2 秒更新一次，超过 90 秒没更新就判定卡死并重启。
  - `.env` 改动后，空仓时自动重启 bot。
  - 控制台 127.0.0.1:8765 也由它看着。
- **Memoh hooks**：在 `/data/.memoh/hooks.json` 里，名字以 `hl-news-bot:` 开头。
  - **SessionStart**（每个新会话，包括每次定时任务运行）：`hlbot ensure --hook`。工作区重启后主要靠它把 bot 拉起来。
  - **WorkspaceStart**（Memoh 创建或重建工作区时）：`hlbot ensure --hook`。
  - **WorkspaceStop**（Memoh 自己停止工作区时）：`hlbot stop --hook`，平仓退出，但不记为手动停止。
  - 注意：按 Memoh v1.0.0-beta.1 的代码，Workspace 标签页的 Start / Stop 按钮和服务器重启后的自动恢复都**不会**触发 WorkspaceStart / WorkspaceStop。所以点 Stop 前要先运行 `hlbot stop`，否则进程会被直接杀掉、来不及平仓；模拟盘的这些持仓会在下次启动时记为 restart_abandoned，并发一条告警。
- **定时任务**：每小时一次。每次都会开一个新会话，SessionStart hook 就会先把 bot 拉起来，然后你再用 `hlbot ensure --quiet` 确认一遍。
- **相关文件**（都在 `data/` 下）：
  - 状态：`supervisor.json`（守护进程状态）、`supervisor.opts.json`（启动方式和套餐）、`supervisor.disabled`（手动停止标记）
  - 日志：`supervisor.log`（守护进程）、`bot.out`（bot 输出，包括崩溃堆栈）、`hlnews.log`（bot 日志）、`ui.out`（控制台）
- **控制台**：在 Memoh 的 **Desktop** 标签页打开浏览器，访问 http://127.0.0.1:8765。它只监听本机，不对外开放。
