---
name: mihomo-troubleshoot
description: >
  快速排查并修复 mihomo 代理故障。当用户报告任何海外服务访问失败
  （"xxx 登不上"、"xxx 更新失败 403"、"是不是代理问题"、"切个节点"等），
  且本机使用 mihomo 作为代理时触发。常见目标：codex、claude、github、
  openai、anthropic api、cargo、npm 等任何走海外域名的工具。
---

# mihomo 代理故障排查

本机环境：
- mihomo 工作目录 `/home/yjc/.config/mihomo/`，配置文件 `config.yaml`
- 代理端口 `7890`（HTTP/SOCKS5），TUN 默认关闭 → curl 须 `-x http://127.0.0.1:7890`
- external-controller `127.0.0.1:9090`（无 token，本机直连）
- `profile.store-selected: true` → 切换的节点会持久化，重启不丢

---

## 三步流程

### 1. 看 mihomo 日志，判断故障层

```bash
journalctl -u mihomo --no-pager --since "30 min ago" | grep -iE "error|warn|fail|timeout|reset" | tail -20
```

- **有 error/timeout** → 代理层故障（节点挂、握手失败），进入第 3 步直接换节点
- **无 error，但应用报错（403/reset 等）** → 应用层故障（节点 IP 被目标站点风控），mihomo 自身不打印 HTTP 响应码，**"日志没报错" ≠ "代理没问题"**

### 2. 定位走的代理组

```bash
# 看目标域名最近的代理路径
journalctl -u mihomo --no-pager --since "10 min ago" | grep "域名关键字" | tail -5
# 输出格式: [TCP] src --> domain:443 match RuleSet(...) using Group[节点名]
```

对照 `config.yaml` 的 `rules:` 反查（OpenAI/codex/多数海外服务走 `其他` 组，github 走 `Github` 组，Anthropic 走 `Anthropic` 组，等等）。

**注意重定向跨组**：一次请求可能跨多个组（如 `chatgpt.com` → `github.com`），用 `curl -L -v` 看清哪一跳失败。

### 3. 切到稳定节点并验证

```bash
# 查候选地区自动选择组的当前节点
curl -s http://127.0.0.1:9090/proxies | python3 -c "
import json,sys
d=json.load(sys.stdin)['proxies']
for k in ['美国自动选择','日本自动选择','新加坡自动选择','香港自动选择','台湾自动选择']:
    if k in d: print(f'{k} → {d[k].get(\"now\",\"?\")}')"

# 测延迟（用目标域名本身，不要用默认 cp.cloudflare.com）
URL="https://目标域名/"
for g in 美国自动选择 日本自动选择 新加坡自动选择; do
  enc=$(python3 -c "import urllib.parse;print(urllib.parse.quote('$g'))")
  echo "$g: $(curl -s --max-time 10 "http://127.0.0.1:9090/proxies/$enc/delay?url=$URL&timeout=5000")"
done

# 切组
G="其他"; T="美国自动选择"   # T 可以是子组名或具体节点名
curl -s -X PUT "http://127.0.0.1:9090/proxies/$(python3 -c "import urllib.parse;print(urllib.parse.quote('$G'))")" \
  -H "Content-Type: application/json" -d "{\"name\":\"$T\"}" -w "切换 HTTP %{http_code}\n"

# 验证（至少连测 5 次，风控有抖动）
for i in 1 2 3 4 5; do
  curl -sS -o /dev/null -w "%{http_code} " --max-time 10 -x http://127.0.0.1:7890 "https://目标域名/"
done; echo
```

---

## 选节点的判断依据

延迟低 ≠ 能用。**节点 IP 类型比延迟更重要**：

- 数据中心 IP / 共享 IEPL：容易被风控，对 OpenAI/Cloudflare 类站点经常 403
- 家庭/住宅 IP（节点名通常含"家庭"、"住宅"、"5倍消耗"）：风控最宽松

美国服务（OpenAI/Anthropic/GitHub/Google）优先美国家庭 IP。

判定响应：
- 200/302/401/404 → 代理通，问题在应用本身
- 403 + `server: cloudflare` → 节点 IP 被风控，换节点类型
- 反复 200/403 交替 → 共享 IP 抖动，换独享或家庭 IP

---

## 输出格式

排查完给三件事：

1. **根因**：哪一跳、哪个节点、什么类型的故障（代理层 / IP 风控 / 地区限制 / 上游真挂）
2. **当前状态**：已切到什么节点，连测 5 次的结果
3. **长期建议**：该服务推荐固定走哪类节点
