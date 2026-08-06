1. 终端进入当前目录，然后执行：(创建镜像) gdal版本为
```shell
docker build -t gdal_py -f Dockerfile_BASE .
```
2. 运行(交互的形式)
```shell
docker run -it gdal_py /bin/bash
# 查看gdal
gdalinfo --version
```