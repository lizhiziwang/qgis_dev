#!/usr/bin/env bash
# /opt/qgis_app 安装版启动验证
export DISPLAY=:0
export XAUTHORITY=/home/zsh/.Xauthority
echo "=== /opt/qgis_app 关键文件 ==="
ls -la /opt/qgis_app/qgis_dev /opt/qgis_app/launch_qgis.sh 2>&1
echo "=== .desktop ==="
cat /usr/share/applications/qgis-dev-app.desktop
echo ""
echo "=== 启动 ==="
nohup /opt/qgis_app/launch_qgis.sh > /tmp/qgis_opt.log 2>&1 &
echo "launched pid=$!"
sleep 25
echo "=== 进程 ==="
ps aux | grep '[q]gis_dev' | grep -v grep | head -2
echo "=== 启动日志 ==="
head -20 /tmp/qgis_opt.log 2>/dev/null
echo "=== 日志目录（/opt 只读回退）==="
ls -la /opt/qgis_app/logs/ 2>/dev/null | head -4
ls -la ~/.local/share/qgis_dev/logs/ 2>/dev/null | head -4
