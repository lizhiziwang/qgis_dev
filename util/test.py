import subprocess
import sys
import tempfile
from pathlib import Path

# 定义路径
input_file = r'D:\zsh\work\pyh\20251007\HJ2A_CCD1_E115.9_N29.3_20251007_L1A0001642014\HJ2A_CCD1_E115.9_N29.3_20251007_L1A0001642014.tiff'
output_dir = r'D:\Temp\xyz'

# 确保输入文件存在
if not Path(input_file).exists():
    raise FileNotFoundError(f"Input file not found: {input_file}")

vrt_path = r'D:\Temp\converted.vrt'

translate_cmd = [
    sys.executable,
     "gdal_translate",
    "-of", "VRT",
    "-ot", "Byte",
    "-scale",  # 自动拉伸
    input_file,
    vrt_path
]
subprocess.run(translate_cmd, check=True)


# 构建命令（使用短选项更兼容）
command = [
    sys.executable,
    "-m", "osgeo_utils.gdal2tiles",
    "-z", "0-10",
    "--processes", "4",               # 注意：有些版本用 -p 表示进程数（不是 --processes）
    "-r", "bilinear",
    "--webviewer", "none",
    vrt_path,
    output_dir
]

# 执行
try:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    print("Success:", result.stdout)
except subprocess.CalledProcessError as e:
    print("Error:", e.stderr)
    raise