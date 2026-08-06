# Docker 多阶段构建：完全自包含的 Linux 端 PyQGIS + qgis_dev
#
# 用法：
#   docker build -f tools/docker/Dockerfile -t qgis-app .
#   docker run --rm -v $(pwd):/out qgis-app cp -r /app /out/

FROM continuumio/miniforge3 AS builder

ARG PYTHON_VERSION=3.10
ARG APP_NAME=app

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

ENV PATH=/opt/conda/envs/qgis-env/bin:$PATH
ENV CONDA_DEFAULT_ENV=qgis-env

WORKDIR /build
COPY . .

# PyInstaller
RUN python -m PyInstaller \
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
    app/main.py

# 运行时镜像
FROM ubuntu:22.04

ARG APP_NAME=app

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

CMD ["/app/app"]
