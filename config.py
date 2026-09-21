"""
WMTS 瓦片下载与拼接 - 统一配置文件
在此文件中配置参数，download_tiles.py 和 merge_tiles.py 将自动读取
"""

# from string import Template

# https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer?layer=WMTS020101010000045&style=default&tilematrixset=EPSG:4326&Service=WMTS&Request=GetTile&Version=1.0.0&Format=image/png&TileMatrix=16&TileCol=52838&TileRow=10496
BASE_URL = "https://geocloud.hubgs.com/api/igs/rest/ogc/WMTSServer"
LAYER = "WMTS020101010007006"
STYLE = "default"
TILEMATRIXSET = "EPSG:4326"
SERVICE = "WMTS"
REQUEST = "GetTile"
VERSION = "1.0.0"
FORMAT = "image/png"

# ============ 下载范围配置 ============
TILE_MATRIX = 16  # 缩放级别
TILE_COL_START = 53248  # 列起始
TILE_COL_END = 53521  # 列结束
TILE_ROW_START = 10558  # 行起始
TILE_ROW_END = 10740  # 行结束

# ============ 下载设置 ============
OUTPUT_DIR = "tiles"  # 下载保存目录
MAX_WORKERS = 16  # 并发下载数
TIMEOUT = 10  # 请求超时时间（秒）

# ============ 拼接设置 ============
OUTPUT_FILE = "merged_map.png"  # 输出文件名

# ============ 请求头（模拟浏览器） ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": "_pk_ref.1.4b08=%5B%22%22%2C%22%22%2C1775822146%2C%22https%3A%2F%2Fwww.baidu.com%2Flink%3Furl%3DyqrlszB505EkvnUzlbW0TENQ24OAAmLUe7QA8jFFyeg0x-rSCix4R2s_mywsjH17%26wd%3D%26eqid%3Ded6eec80009143150000000569a30073%22%5D; _pk_id.1.4b08=decaee2f2e207935.1772290171.; _pk_id.8.4b08=3e10c5792cde8133.1772290182.; hb_token=eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJjb20uem9uZHkuc2hpcm8iLCJpc3MiOiJjb20uem9uZHkuc2hpcm8iLCJpYXQiOjE3NzU4MjIyNzEsInN1YiI6IjYyMTAiLCJleHAiOjE3NzU4MjQ5NzF9.R5Ec_GSQnh2wWczPWOBJsPsjN8UHIZpu3TKeoG57dVk; userCenterAccount=420323199512012823; uid=1367685N710880615; _pk_ses.1.4b08=1; _pk_ses.8.4b08=1",
}


def get_range_info():
    """获取下载范围信息，用于显示"""
    cols = TILE_COL_END - TILE_COL_START + 1
    rows = TILE_ROW_END - TILE_ROW_START + 1
    total = cols * rows
    return {"cols": cols, "rows": rows, "total": total, "level": TILE_MATRIX}


def get_tile_url(tile_matrix, tile_col, tile_row):
    """构建瓦片下载URL"""
    params = {
        "layer": LAYER,
        "style": STYLE,
        "tilematrixset": TILEMATRIXSET,
        "Service": SERVICE,
        "Request": REQUEST,
        "Version": VERSION,
        "Format": FORMAT,
        "TileMatrix": tile_matrix,
        "TileCol": tile_col,
        "TileRow": tile_row,
    }
    return f"{BASE_URL}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
