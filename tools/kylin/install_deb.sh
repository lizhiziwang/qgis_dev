#!/usr/bin/env bash
# 银河麒麟 V10 一键安装脚本（需 root）：
#   1. 关闭 dpkg 的 uid 黑白名单校验（麒麟 V10 会拒绝无 _kylin.sig 签名的 deb）
#   2. 安装 qgis-dev-app 的 .deb
#
# 用法：
#   sudo bash install_deb.sh qgis-dev-app_0.2.0_amd64.deb
#   （把 .deb 和本脚本放同一目录，或给 .deb 完整路径）
#
# 背景：麒麟 V10 SP1/SP2（Ubuntu 20.04 底座）的 dpkg 打了国产化校验补丁
#   （libkylin_bwlist/libkylin_signtool）。kylin 官方仓库的 deb 带
#   _kylin.crt/_kylin.sig 签名成员；普通开发者没有 UKey 硬件和 CA 私钥，
#   无法给 deb 签名，无签名包会被 /etc/dpkg/udeblist.xml（bw_status="2"）
#   的 uid 黑白名单拦截，报 "verification failed, refuse to install"。
#   本脚本把 bw_status 改为 0（关闭该校验），原配置备份到 .bak。

set -euo pipefail

DEB="${1:-}"
if [ -z "$DEB" ] || [ ! -f "$DEB" ]; then
    echo "用法: sudo bash install_deb.sh <qgis-dev-app_*.deb>" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行：sudo bash $0 $DEB" >&2
    exit 1
fi

# ---- 1. 关闭麒麟 dpkg 黑白名单校验（若存在且处于白名单严格模式）----
if [ -f /etc/dpkg/udeblist.xml ] && grep -q 'bw_status="2"' /etc/dpkg/udeblist.xml; then
    echo "== 关闭 /etc/dpkg/udeblist.xml 黑白名单校验（bw_status 2 -> 0）"
    cp /etc/dpkg/udeblist.xml /etc/dpkg/udeblist.xml.bak
    sed -i 's/bw_status="2"/bw_status="0"/' /etc/dpkg/udeblist.xml
    echo "   原配置已备份: /etc/dpkg/udeblist.xml.bak"
fi

# ---- 2. 安装 ----
echo "== 安装 $DEB"
dpkg -i "$DEB"

echo ""
echo "=============================================="
echo "  安装完成！"
echo "  桌面菜单/开始菜单搜索『图斑核实平台』启动"
echo "  或命令行：/opt/qgis_app/launch_qgis.sh"
echo "  卸载：dpkg -r qgis-dev-app"
echo "=============================================="
