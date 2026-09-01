#!/usr/bin/env bash
# 便携包试跑（真机桌面 :0）
export DISPLAY=:0
export XAUTHORITY=/home/zsh/.Xauthority
cd /home/zsh/桌面/qgis_dev
pwd
ls -la dist/app/launch_qgis.sh
nohup dist/app/launch_qgis.sh > /tmp/qgis_app_launch.log 2>&1 &
echo "launched pid=$!"
sleep 25
echo "=== 进程 ==="
ps aux | grep '[d]ist/app/app' | head -2
echo "=== 启动日志 ==="
head -50 /tmp/qgis_app_launch.log 2>/dev/null
echo "=== 应用日志目录 ==="
ls -la dist/app/logs/ 2>/dev/null | head -8
