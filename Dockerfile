# DXF_3D —— 独立运行镜像（FreeCAD + matplotlib + openai SDK）
#
# 构建：
#     cd DXF_3D
#     docker build -t dxf-3d .
#
# 运行：
#     ./run.sh DXF_3D/dxf_files/Drawing1.dxf
# 或在容器外目录处理任意 dxf：
#     ./run.sh /path/to/some.dxf
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONPATH=/app

# ---------------------------------------------------------------------------
# 系统依赖：FreeCAD（提供 freecadcmd / FreeCAD / Part / Mesh / MeshPart）
# ---------------------------------------------------------------------------
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg2 software-properties-common \
        python3 python3-pip \
        fonts-dejavu fonts-liberation \
        libgl1 libsm6 libxext6 libxrender1 \
 && add-apt-repository ppa:freecad-maintainers/freecad-stable -y \
 && apt-get update -y \
 && apt-get install -y --no-install-recommends freecad \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python 依赖
# ---------------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt /app/DXF_3D/requirements.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip \
 && python3 -m pip install --no-cache-dir -r /app/DXF_3D/requirements.txt

# ---------------------------------------------------------------------------
# 业务代码
# ---------------------------------------------------------------------------
COPY . /app/DXF_3D/

# 默认入口：跑 dxf_files/ 下所有 DXF
WORKDIR /app/DXF_3D
CMD ["freecadcmd", "-c", "import sys; sys.path.insert(0,'/app'); from DXF_3D.run import main; sys.exit(main())"]
