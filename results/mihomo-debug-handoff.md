# mihomo 调试交接（给 Astra）

记录时间：2026-09-10。以下是本次调试后的状态，后续操作前仍需核对实时配置。

- **用户要求不要使用现有 `mihomo-troubleshoot` skill**，里面的端口和分流假设已经过时。先看运行参数、配置、日志，不要直接换节点。
- systemd 服务名 `mihomo`，启动参数是 `/usr/bin/mihomo -d /home/yjc/.config/mihomo`；配置为 `~/.config/mihomo/config.yaml`，显式代理端口 **7897**。
- **MetaCubeXD 是 mihomo 内置托管的面板**：`external-controller: 127.0.0.1:8765`、`external-ui: ui`，有 `secret`。原密码已从 `config.yaml.bak.20260826184515` 恢复，不要在输出中打印密码。用户通过内网穿透访问 `http://<公网服务器IP>:8765/ui/`，面板后端也是该公网地址的 **8765**，不是远程浏览器自己的 localhost。穿透工具和映射细节尚未核实。
- **不要另起 Python 静态服务器占用 8765**。此前误启动的用户级临时服务 `metacubexd` 已停止；它只有页面、没有控制 API。旧配置备份证明面板与 API 原本共用 mihomo 的 8765。恢复配置后检查通过，重启交给用户执行，恢复后的公网链路未在本次记录中验证。
- **TUN 保持关闭**。当前会话的 `http_proxy` / `https_proxy` 指向另一个正在监听的 `127.0.0.1:1081`。开启 TUN 自动路由后 Codex 超时，关闭后用户确认恢复。可确认问题与 TUN 接管有关；“1081 的出站又被 mihomo 代理”只是推断，未证实。TUN 自动路由通常影响同一网络命名空间的所有用户，不仅是启动者。
- 曾经无法启动的直接原因是 **GeoSite.dat 无效/缺少 cn 集合，重新下载又超时**。已将 DNS `nameserver-policy` 的 `geosite:cn`、`geosite:geolocation-!cn` 改为 `rule-set:cn_domain`、`rule-set:geolocation-!cn`，复用配置已有的 MRS 规则集。配置检查通过；数据文件为何损坏未查明。另删除了弃用的 `global-client-fingerprint`，该警告本身不是启动失败原因。
- 最后一次查 Claude 的日志（9 月 9 日 23:57–23:58）：`api.anthropic.com → RuleSet(anthropic_domain) → Anthropic[美国-直播节点]`。这是当时的实际连接，不代表以后始终如此。

常用检查（只读；`-t` 可能下载依赖或访问缓存）：

```bash
systemctl show mihomo -p ActiveState -p SubState -p ExecStart
mihomo -t -d ~/.config/mihomo -f ~/.config/mihomo/config.yaml
ss -ltnp 'sport = :8765 or sport = :7897 or sport = :1081'
journalctl -u mihomo -n 40 --no-pager
journalctl -u mihomo --since '10 min ago' --no-pager | rg -i 'anthropic|claude'
```

查本地 HTTP 时用 `curl --noproxy '*'`，避免环境代理干扰；8765 API 已恢复认证，无凭据返回 401 不等于后端不可达。`systemctl active` 也不等于初始化完成，要结合日志、监听和 API 响应。浏览器还能显示缓存面板不等于服务器在监听。配置改动先备份再 `-t`；sudo 在本会话需要用户输入密码，不要声称未执行的重启已完成。
