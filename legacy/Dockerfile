# ============================================================
# Docker 多阶段构建：Linux 便携 PyQGIS 应用
# 完全不依赖宿主机任何 QGIS 组件
# ============================================================

# ---- Stage 1: 构建环境 ----
FROM continuumio/miniforge3 AS builder

ARG APP_NAME=app
ARG PYTHON_VERSION=3.10

# 创建 conda 环境
RUN conda create -n qgis-env -c conda-forge -y \
    python=$PYTHON_VERSION \
    qgis \
    pyinstaller \
    pyqt=5 \
    rasterio \
    fastapi \
    uvicorn \
    networkx \
    matplotlib \
    && conda clean --all -y

# 设置 conda 环境路径
ENV PATH=/opt/conda/envs/qgis-env/bin:$PATH
ENV CONDA_DEFAULT_ENV=qgis-env

WORKDIR /build
COPY . .

# 运行 PyInstaller
RUN python3 -m PyInstaller \
    --noconfirm \
    --name "$APP_NAME" \
    --onedir \
    --windowed \
    --hidden-import=rasterio._features \
    --hidden-import=osgeo \
    --hidden-import=osgeo.gdal \
    --hidden-import=osgeo.ogr \
    --hidden-import=osgeo.osr \
    --hidden-import=qgis.core \
    --hidden-import=qgis.gui \
    --hidden-import=qgis.utils \
    --hidden-import=PyQt5 \
    --hidden-import=PyQt5.QtCore \
    --hidden-import=PyQt5.QtGui \
    --hidden-import=PyQt5.QtWidgets \
    --hidden-import=PyQt5.QtNetwork \
    --hidden-import=PyQt5.QtXml \
    --hidden-import=PyQt5.QtSvg \
    --hidden-import=PyQt5.QtSql \
    --collect-all rasterio \
    --collect-all setuptools \
    --collect-all qgis \
    --add-data /opt/conda/envs/qgis-env/lib/python${PYTHON_VERSION}/site-packages/qgis:qgis \
    --add-data resource:resource \
    --add-binary /opt/conda/envs/qgis-env/lib/libqgis_core.so:. \
    --add-binary /opt/conda/envs/qgis-env/lib/libqgis_gui.so:. \
    --add-binary /opt/conda/envs/qgis-env/lib/libgdal.so:. \
    --add-binary /opt/conda/envs/qgis-env/lib/libgeos_c.so:. \
    --add-binary /opt/conda/envs/qgis-env/lib/libproj.so:. \
    --add-binary /opt/conda/envs/qgis-env/lib/libsqlite3.so:. \
    main.py

# ---- Stage 2: 运行镜像 ----
FROM ubuntu:22.04

ARG APP_NAME=app

# 只安装最低限度的运行时系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1-mesa-glx \
    libxcb-xinerama0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxcb-xkb1 \
    libxkbcommon-x11-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/dist/$APP_NAME /app

# 对于非 Docker 运行场景，可将此镜像中的 /app 目录导出为便携包
# 命令: docker build -t qgis-app . && docker run --rm -v $(pwd):/out qgis-app cp -r /app /out/
CMD ["/app/app"]
