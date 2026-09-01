#!/usr/bin/env python
"""SSH 调试麒麟真机（zsh@192.168.191.130）的辅助工具。

用法:
    python tools/ssh_kylin.py "命令..."        # 在真机执行一条命令
    python tools/ssh_kylin.py --shell          # 交互式 shell（一次会话多条命令）
    python tools/ssh_kylin.py --script file.sh # 把本地脚本内容在真机执行
"""
import sys
import time

import paramiko

HOST = "192.168.191.130"
USER = "zsh"
PASS = "Aa@123456"


def get_client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=20)
    return c


def run_one(c, cmd, timeout=600):
    """执行命令，合并 stdout/stderr，返回 (exit_code, output)。"""
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    c = get_client()
    if args[0] == "--shell":
        print(f"交互式 shell @ {USER}@{HOST}（输入 exit 退出）")
        while True:
            cmd = input("kylin$ ").strip()
            if not cmd:
                continue
            if cmd in ("exit", "quit"):
                break
            t0 = time.time()
            code, out = run_one(c, cmd)
            dt = time.time() - t0
            print(out)
            print(f"[exit={code}  {dt:.1f}s]")
    elif args[0] == "--script":
        with open(args[1], encoding="utf-8") as f:
            script = f.read()
        code, out = run_one(c, script)
        print(out)
        print(f"[exit={code}]")
    else:
        cmd = " ".join(args)
        code, out = run_one(c, cmd)
        print(out)
        print(f"[exit={code}]")
    c.close()


if __name__ == "__main__":
    main()
