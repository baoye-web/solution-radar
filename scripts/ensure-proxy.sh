#!/usr/bin/env bash
# scripts/ensure-proxy.sh — 保证「当前生效的网络服务」上挂着 Clash 代理
#
# 背景（2026-11-02 实测，故障已复现并修复）：
#   机器同时连着 Ethernet(en0) 与 Wi-Fi(en1)，默认路由走 en0。
#   Clash Verge 只把系统代理写到了 **Wi-Fi** 服务上（127.0.0.1:7897），
#   Ethernet 服务上是空的 → Chrome/ego-browser 直连 → Gmail 超时
#   （ERR_CONNECTION_TIMED_OUT），取验证码通道静默失效。
#   判定依据：scutil --proxy 的 HTTPEnable=0 但 networksetup -getwebproxy "Wi-Fi" 显示 Enabled:Yes。
#
# 做法：
#   1. 取默认路由接口（如 en0）
#   2. 反查它对应的 macOS 网络服务名（如 Ethernet）
#   3. 若该服务上没有代理，则把 Clash 的 mixed-port 复制过去
#   4. 幂等：已配置则什么都不做
#
# 用法：bash scripts/ensure-proxy.sh [--quiet]
set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = "1" ] || echo "[$(date +%H:%M:%S)] $*" >&2; }

# 1. 默认路由接口
IFACE=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')
if [ -z "${IFACE:-}" ]; then
  say "无法确定默认路由接口，跳过代理检查"
  exit 0
fi

# 2. 接口 → 网络服务名
#    输出格式：
#      (1) Ethernet
#      (Hardware Port: Ethernet, Device: en0)
#    服务名在 "(N) 名字" 行，设备在下一行的 "Device: enN"。
SERVICE=$(networksetup -listnetworkserviceorder 2>/dev/null | tr -d '\r' | \
  awk -v want="$IFACE" '
    /^\([0-9]+\)/ {
      name = $0
      sub(/^\([0-9]+\)[ \t]*/, "", name)   # 去掉 "(N) " 前缀，保留服务名
      sub(/[ \t]+$/, "", name)
    }
    /Device:/ {
      dev = $0
      sub(/.*Device:[ \t]*/, "", dev)       # 取出 enN
      sub(/[ \t,)].*$/, "", dev)             # 去掉行尾的 ")" 等残留
      if (dev == want) { print name; exit }
    }
  ')

if [ -z "${SERVICE:-}" ]; then
  say "接口 $IFACE 未匹配到网络服务名，跳过"
  exit 0
fi

# 3. 该服务是否已有可用代理（Enabled=Yes 且端口非 0）
#    ⚠️ 取值必须用 '^Enabled:' 精确匹配并取**首行**：
#    networksetup 输出里还有一行 "Authenticated Proxy Enabled: 0"，
#    用 /Enabled:/ 会把那一行也命中，取末行就得到 "0" → 误判为「未配置」。
CFG=$(networksetup -getwebproxy "$SERVICE" 2>/dev/null | tr -d '\r')
ENABLED=$(printf '%s\n' "$CFG" | awk -F': *' '/^Enabled:/{print $2; exit}')
CURPORT=$(printf '%s\n' "$CFG" | awk -F': *' '/^Port:/{print $2; exit}')
if [ "$ENABLED" = "Yes" ] && [ "${CURPORT:-0}" -gt 0 ] 2>/dev/null; then
  say "代理已就位：服务=$SERVICE 接口=$IFACE 端口=$CURPORT"
  exit 0
fi

# 4. 从 Clash 运行时配置里取 mixed-port，复制到当前服务
#    注意：不能用 lsof 判断端口是否在监听 —— mihomo 内核跑在 root 的特权 helper 里，
#    普通用户的 lsof 看不到它的 socket（实测 7897 在监听但 lsof 为空）。
#    改用「真实建连」来判定端口可用。
CLASH_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
port_alive() {
  # $1 = 端口。用 bash 内建 /dev/tcp 做一次真实 TCP 连接。
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1 && exec 3<&- && exec 3>&- && return 0
  return 1
}
PORT=""
for f in "$CLASH_DIR/config.yaml" "$CLASH_DIR"/profiles/*.yaml; do
  [ -f "$f" ] || continue
  for P in $(grep -E '^(mixed-port|socks-port|port):' "$f" 2>/dev/null | tr -dc '0-9\n' | tr '\n' ' '); do
    if port_alive "$P"; then PORT="$P"; break; fi
  done
  [ -n "$PORT" ] && break
done

if [ -z "$PORT" ]; then
  say "未找到可用的 Clash 端口（代理核心可能未启动），跳过"
  exit 0
fi

networksetup -setwebproxy "$SERVICE" 127.0.0.1 "$PORT" >/dev/null 2>&1
networksetup -setsecurewebproxy "$SERVICE" 127.0.0.1 "$PORT" >/dev/null 2>&1
networksetup -setsocksfirewallproxy "$SERVICE" 127.0.0.1 "$PORT" >/dev/null 2>&1

NEW=$(networksetup -getwebproxy "$SERVICE" 2>/dev/null | tr -d '\r' | awk -F': *' '/^Enabled:/{print $2; exit}')
if [ "$NEW" = "Yes" ]; then
  say "✅ 已把 Clash 代理（127.0.0.1:${PORT}）补挂到当前服务「${SERVICE}」（接口 ${IFACE}）"
else
  say "⚠️ 代理写入失败：服务=$SERVICE"
fi
exit 0
