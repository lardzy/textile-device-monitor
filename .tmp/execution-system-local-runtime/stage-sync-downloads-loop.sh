#!/bin/bash
# 开发机联调专用：把执行系统 staging 目录镜像到 ~/Downloads/exec-stage-sync，
# 供 Parallels Windows VM 的写入桥经 Z:\Downloads 读取（Shared Profile 通道）。
# 生产部署不使用本脚本（桥与容器同机时直接映射宿主机目录）。
set -u
SRC="/Users/lishuyang/PycharmProjects/textile-device-monitor/.tmp/execution-system-local-runtime/execution-runtime/"
DST="/Users/lishuyang/Downloads/exec-stage-sync/"
mkdir -p "$DST"
while true; do
  rsync -a --delete --delay-updates "$SRC" "$DST" 2>/dev/null
  sleep 2
done
