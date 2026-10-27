#!/usr/bin/env bash
# 用途：逐个检查网站当前提供的 HTTPS 证书，输出到期时间和剩余天数。
# 用法：填写下方 DOMAINS，在 Nginx 所在服务器运行 bash check_https_certs.sh。
# 可从任意工作目录执行；本脚本不使用相对文件路径。
# 默认连接 127.0.0.1:443，通过 SNI 选择各域名的源站证书，绕过 Cloudflare。
# 退出码 0：所有网站距证书到期都严格超过 WARN_DAYS（默认 14 天）。
# 退出码 1：任一网站剩余时间 <= 14 天、已过期或读取失败；仍会检查其余网站。
# 退出码 2：域名列表为空或缺少依赖。修改 WARN_DAYS 后按新阈值判断。
# 只检查证书到期时间，不读取 acme.sh 配置；普通账户运行，无需 sudo。
# 运行环境：Linux，依赖 OpenSSL、GNU date 和 timeout；输出时间使用日本时间。
set -uo pipefail

SCRIPT_VERSION="v0.1.0"

# ---------- 按需修改这里 ----------
# 每行一个域名，不加 https:// 或路径。
DOMAINS=(
    "wiki.gzher.com"
    "lib.gzher.com"
    # "example.com"
    # "www.example.com"
)
# 留空：按域名访问；开启 Cloudflare 橙云代理时查到的是 Cloudflare 证书。
# 填 127.0.0.1：在 Nginx 所在服务器运行，直接检查源站已加载的证书。
CONNECT_HOST="127.0.0.1"
PORT=443
TIMEOUT_SECONDS=10
WARN_DAYS=14

check_dependencies() {
    local tool
    for tool in openssl date timeout; do
        if ! command -v "$tool" >/dev/null; then
            printf '缺少命令：%s\n' "$tool" >&2
            return 2
        fi
    done
    if ! date -d '@0' +%s >/dev/null 2>&1; then
        printf '需要 GNU date，请在 Linux 服务器运行。\n' >&2
        return 2
    fi
}

check_site() {
    local domain=$1 target response expires epoch expires_jst remaining days hours status
    target=${CONNECT_HOST:-$domain}

    # SNI 用于同一个 IP 上的多个网站；超时避免自动任务一直挂起。
    # 若已取得证书，连接关闭时的 EOF 错误不影响到期时间的解析。
    response=$(timeout --kill-after=2s "${TIMEOUT_SECONDS}s" \
        openssl s_client -connect "$target:$PORT" -servername "$domain" \
        -showcerts </dev/null 2>/dev/null) || true

    if ! expires=$(openssl x509 -noout -enddate <<< "$response" 2>/dev/null); then
        printf '%s | 读取失败（连接超时、TLS 失败或未返回证书）\n' "$domain"
        return 1
    fi
    expires=${expires#notAfter=}
    if ! epoch=$(LC_ALL=C date -d "$expires" +%s 2>/dev/null); then
        printf '%s | 无法解析证书到期时间\n' "$domain"
        return 1
    fi

    expires_jst=$(TZ=Asia/Tokyo date -d "@$epoch" '+%Y-%m-%d %H:%M:%S JST')
    remaining=$((epoch - $(date +%s)))
    if (( remaining <= 0 )); then
        printf '%s | 到期：%s | 已过期\n' "$domain" "$expires_jst"
        return 1
    fi

    days=$((remaining / 86400))
    hours=$((remaining % 86400 / 3600))
    status="未到期"
    if (( remaining <= WARN_DAYS * 86400 )); then
        status="即将到期"
    fi
    printf '%s | 到期：%s | 剩余：%s 天 %s 小时 | %s\n' \
        "$domain" "$expires_jst" "$days" "$hours" "$status"
    [[ "$status" == "未到期" ]]
}

main() {
    printf '%s %s\n' "${BASH_SOURCE[0]##*/}" "$SCRIPT_VERSION"
    check_dependencies || return $?
    if (( ${#DOMAINS[@]} == 0 )); then
        printf '请先在脚本开头填写 DOMAINS。\n' >&2
        return 2
    fi

    printf '[%s] HTTPS 证书到期检查，共 %s 个网站，连接目标：%s:%s\n' \
        "$(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S JST')" "${#DOMAINS[@]}" \
        "${CONNECT_HOST:-各域名的解析地址}" "$PORT"
    local domain result=0
    for domain in "${DOMAINS[@]}"; do
        check_site "$domain" || result=1
    done
    # 0：全部未进入提醒期；1：有临期、过期或读取失败；2：配置/依赖问题。
    return "$result"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
