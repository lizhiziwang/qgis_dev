# 使用官方 Temurin JDK 8 + Ubuntu 20.04（focal）
FROM eclipse-temurin:8-jdk-focal

# 设置环境变量
ENV GDAL_VERSION=3.9.2
ENV PREFIX=/usr/local
# ENV CLASSPATH="${PREFIX}/lib/gdal.jar:${CLASSPATH}"
# ENV CLASSPATH="${PREFIX}/share/java/gdal.jar:${PREFIX}/lib/gdal.jar:${CLASSPATH}"

ENV LD_LIBRARY_PATH="${PREFIX}/lib:${LD_LIBRARY_PATH}"
# ENV PATH="/usr/lib/jvm/java-8-openjdk-amd64/bin:${PREFIX}/bin:${PATH}"
ENV PATH="${PREFIX}/bin:${PATH}"

# 禁用 numpy
#ENV GDAL_PYTHON_BINDINGS_WITHOUT_NUMPY=1

# 使用阿里云源
RUN sed -i 's/archive.ubuntu.com/mirrors.aliyun.com/g' /etc/apt/sources.list && \
    sed -i 's/security.ubuntu.com/mirrors.aliyun.com/g' /etc/apt/sources.list

# 安装构建依赖（包含 ant！）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        ninja-build \
        swig \
        pkg-config \
        libproj-dev \
        libgeos-dev \
        libsqlite3-dev \
        zlib1g-dev \
        libjpeg-dev \
        libpng-dev \
        libcurl4-openssl-dev \
        python3-dev \
        python3-setuptools \
        python3-wheel \
        ca-certificates \
        ant \
    && rm -rf /var/lib/apt/lists/*

# 验证 Java 和头文件
RUN java -version && javac -version && echo "JAVA_HOME: $JAVA_HOME"

# ✅ 验证 JNI 头文件是否存在
#RUN echo "✅ Checking JNI headers..." && \
#    ls -l $JAVA_HOME/include/jni.h && \
#    ls -l $JAVA_HOME/include/linux/jni_md.h

# 验证 ant 是否安装
RUN ant -version

# 工作目录
WORKDIR /opt/gdal-build

# 复制本地 GDAL 源码
RUN wget https://download.osgeo.org/gdal/${GDAL_VERSION}/gdal-${GDAL_VERSION}.tar.gz && \
#    tar -xzf gdal-${GDAL_VERSION}.tar.gz
#COPY gdal-${GDAL_VERSION}.tar.gz ./

# 解压
RUN tar -xzf gdal-${GDAL_VERSION}.tar.gz && \
    mv gdal-${GDAL_VERSION} gdal-src && \
    rm -f gdal-${GDAL_VERSION}.tar.gz

# 创建构建目录
WORKDIR /opt/gdal-build/gdal-src/build

# ✅ CMake 配置：使用正确的变量名和
RUN cmake .. \
    -G Ninja \
    -DCMAKE_INSTALL_PREFIX=${PREFIX} \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_TESTING=OFF \
    -DBUILD_JAVA_BINDINGS=ON \
    -DBUILD_JAVA_ALL_IN_ONE_JNI=ON \
    -DGDAL_USE_PROJ=ON \
    -DGDAL_USE_SQLITE3=ON \
    -DGDAL_USE_PNG=ON \
    -DGDAL_USE_JPEG=ON \
    -DGDAL_USE_ZLIB=ON \
    -DGDAL_USE_TIFF_INTERNAL=ON \
    -DGDAL_USE_GEOTIFF_INTERNAL=ON \
    -DGDAL_USE_CURL=ON \
    -DGDAL_BUILD_PYTHON_BINDINGS=ON \
    -DPYTHON_EXECUTABLE=/usr/bin/python3

# 编译并安装
RUN ninja -j4 && ninja install

# 清理构建目录（可选）
RUN cd / && rm -rf /opt/gdal-build/gdal-src

## ✅ 验证 Java 绑定生成
#RUN echo "✅ Final check for Java bindings:" && \
#    find ${PREFIX}/lib -name "gdal.jar" -o -name "libgdaljni.so" | xargs ls -l

# 验证 GDAL 命令行工具
RUN gdalinfo --version

# 默认命令
CMD ["gdalinfo", "--help"]