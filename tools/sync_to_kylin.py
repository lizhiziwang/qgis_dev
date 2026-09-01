#!/usr/bin/env python3
"""一键同步：本地 push -> zwps dev -> 真机 qgis/ 仓库 git pull origin dev。

工作流（用户约定）：
    本地改 -> git push zwps dev -> 真机 cd ~/桌面/qgis_dev/qgis && git pull origin dev

用法:
    python tools/sync_to_kylin.py                 # 仅同步（要求本地无未提交改动）
    python tools/sync_to_kylin.py --commit "msg"  # 先 git add -A && git commit 再同步

注意: 真机 qgis/ 仓库有未提交改动时不会自动丢弃（安全保护），需手动处理。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import paramiko

REMOTE_URL = "http://zhengshuhao:Aa%40123456@git.zwps.top:7780/zhengshuhao/qgis.git"
BRANCH = "dev"
KYLIN = {"hostname": "192.168.191.130", "username": "zsh", "password": "Aa@123456", "timeout": 20}
KYLIN_REPO = "~/桌面/qgis_dev/qgis"


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"!! 命令失败: {cmd}\n{r.stderr.strip()}")
        sys.exit(1)
    return r.stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", help="先提交所有改动（git add -A && git commit -m ...）")
    args = ap.parse_args()

    root = sh("git rev-parse --show-toplevel")
    os.chdir(root)

    if args.commit:
        sh(f'git add -A && git commit -m "{args.commit}"')

    dirty = sh("git status --porcelain")
    if dirty:
        print("!! 本地有未提交改动，先手动提交或加 --commit '提交信息'：")
        print(dirty)
        sys.exit(1)

    print(f"== 1/2 push -> {BRANCH} (zwps) ==")
    sh(f'git push {REMOTE_URL} {BRANCH}')
    print("push 完成")

    print("== 2/2 真机 qgis/ 仓库 git pull ==")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(**KYLIN)
    try:
        _, out, _ = c.exec_command(f"cd {KYLIN_REPO} && git status --porcelain", timeout=60)
        dirty_remote = out.read().decode("utf-8", "replace").strip()
        if dirty_remote:
            print("!! 真机 qgis/ 仓库有未提交改动，未执行 pull（请先在真机处理）：")
            print(dirty_remote)
            sys.exit(1)

        _, out, err = c.exec_command(
            f'cd {KYLIN_REPO} && git pull "{REMOTE_URL}" {BRANCH} 2>&1', timeout=300
        )
        result = out.read().decode("utf-8", "replace")
        print(result)
        if "Already up to date" in result or "Fast-forward" in result or "files changed" in result:
            print("== 同步完成 ==")
        else:
            print("== 同步结果请人工确认（可能已最新/有冲突）==")
    finally:
        c.close()


if __name__ == "__main__":
    main()
