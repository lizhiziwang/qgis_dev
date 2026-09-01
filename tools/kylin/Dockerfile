# 银河麒麟 V10 / Ubuntu Linux 便携包构建镜像
#
# 底座 Ubuntu 20.04（glibc 2.31）→ 产物兼容麒麟 SP1/SP2/SP3（2.31/2.35）
# 与 Ubuntu 20.04/22.04；架构 = 镜像架构（本文件为 x86_64/兆芯/Intel/AMD）。
#
# 用法（在 Windows / Mac / Linux 有 Docker 的机器上即可，无需麒麟真机）：
#   docker build -t qgis-builder:20.04 tools/kylin
#   docker run -d --name qgis-builder \
#       -v "$(pwd):/workspace" \
#       qgis-builder:20.04 sleep infinity
#   docker exec qgis-builder bash tools/kylin/build_in_docker.sh
#
# 产物：dist/qgis-dev-app_<版本>_amd64.deb + 便携包 dist/app/
# 注：aarch64（飞腾/鲲鹏）目标请改拉 ubuntu:20.04 的 arm64 镜像并在
#     ARM 机器上构建（Docker Desktop 或 qemu 模拟）。

FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

# 构建所需系统工具：wget 拉 miniconda、dpkg-dev 打 .deb、binutils 提供 ldconfig 等
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates bzip2 git dpkg-dev binutils procps \
    && rm -rf /var/lib/apt/lists/*

# Miniconda（linux-64）
RUN wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/mc.sh \
    && bash /tmp/mc.sh -b -p /opt/conda \
    && rm -f /tmp/mc.sh

ENV PATH=/opt/conda/bin:$PATH
# conda 25.x 起默认渠道（pkgs/main 等）强制要求接受 ToS；直接去掉 defaults 只用 conda-forge
RUN conda config --add channels conda-forge \
    && conda config --set channel_priority strict \
    && conda config --set quiet true \
    && conda config --remove channels defaults \
    && conda config --set tos_acceptance true

WORKDIR /workspace
