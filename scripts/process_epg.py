import gzip
import io
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# ==================== 配置区 ====================
def parse_env_list(env_key, default=None):
    """从环境变量解析列表，支持换行、逗号或分号，过滤空行和注释(#)"""
    val = os.getenv(env_key, "").strip()
    if not val:
        return default if default is not None else []
    lines = re.split(r'[\r\n,;]+', val)
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]

# 1. 上游源地址列表（优先从环境变量读取，支持多行/逗号）
SOURCES = parse_env_list("EPG_SOURCES", default=[
    "http://epg.51zmt.top:8000/e.xml",
])

# 2. 本地 m3u 文件路径
M3U_PATH = os.getenv("M3U_PATH", "tv.m3u")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "dist")
OUTPUT_XML = os.path.join(OUTPUT_DIR, "epg.xml")
OUTPUT_GZ = os.path.join(OUTPUT_DIR, "epg.xml.gz")
# ===============================================

def load_channels_from_m3u(file_path):
    """
    解析本地 m3u 文件，提取 tvg-name, tvg-id 以及末尾的频道显示名称
    """
    channels = set()
    if not os.path.exists(file_path):
        print(f" [提示] 未找到 {file_path} 文件，将不限制频道过滤规则（全量合并）。")
        return channels

    print(f"--> 正在从 {file_path} 读取频道列表...")
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("#EXTINF"):
                continue

            # 1. 提取 tvg-name="..."
            tvg_name = re.search(r'tvg-name="([^"]+)"', line, re.IGNORECASE)
            if tvg_name:
                channels.add(tvg_name.group(1).strip())

            # 2. 提取 tvg-id="..."
            tvg_id = re.search(r'tvg-id="([^"]+)"', line, re.IGNORECASE)
            if tvg_id:
                channels.add(tvg_id.group(1).strip())

            # 3. 提取末尾的频道名称 (逗号后面的名称)
            name_part = line.split(",")[-1].strip()
            if name_part and not name_part.startswith("#"):
                channels.add(name_part)

    print(f"[*] 成功从 {file_path} 提取出 {len(channels)} 个频道匹配标识！")
    return channels

def fetch_content(url):
    print(f"--> 正在拉取: {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
            if url.endswith(".gz") or (len(data) > 2 and data[:2] == b'\x1f\x8b'):
                return gzip.decompress(data)
            return data
    except Exception as e:
        print(f" [警告] 下载失败 {url}: {e}")
        return None

def process():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 从根目录 tv.m3u 加载目标频道列表
    target_channels = load_channels_from_m3u(M3U_PATH)

    seen_channels = set()        # 频道 ID 去重
    seen_programmes = set()      # 节目 (channel, start, title) 去重
    matched_channel_ids = set()  # 记录匹配白名单的 channel id

    print(f"[*] 当前配置的上游源数量: {len(SOURCES)}")

    root = ET.Element("tv", {
        "generator-info-name": "GitHub-Actions-EPG-Tool",
        "date": datetime.now().strftime("%Y%m%d%H%M%S")
    })

    # 1. 遍历下载并流式解析 XML
    for url in SOURCES:
        content = fetch_content(url)
        if not content:
            continue

        try:
            context = ET.iterparse(io.BytesIO(content), events=("end",))
            for event, elem in context:
                if elem.tag == "channel":
                    c_id = elem.get("id")
                    names = [dn.text.strip() for dn in elem.findall("display-name") if dn.text]
                    
                    is_match = False
                    if not target_channels:
                        is_match = True
                    else:
                        # 只要 id 或 display-name 任一存在于 m3u 提取出的列表中即保留
                        if (c_id and c_id in target_channels) or any(n in target_channels for n in names):
                            is_match = True

                    if is_match and c_id:
                        matched_channel_ids.add(c_id)
                        if c_id not in seen_channels:
                            seen_channels.add(c_id)
                            root.append(elem)
                    else:
                        elem.clear()

                elif elem.tag == "programme":
                    c_id = elem.get("channel")
                    start = elem.get("start")
                    title_elem = elem.find("title")
                    title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""

                    # 仅保留命中频道的节目，且同一时段标题去重
                    if c_id in matched_channel_ids:
                        prog_key = (c_id, start, title)
                        if prog_key not in seen_programmes:
                            seen_programmes.add(prog_key)
                            root.append(elem)
                    else:
                        elem.clear()

        except Exception as e:
            print(f" [错误] 解析 XML 失败 ({url}): {e}")

    # 2. 导出生成结果
    tree = ET.ElementTree(root)
    print(f"\n[*] 汇总完成: 共保留 {len(seen_channels)} 个频道，{len(seen_programmes)} 条节目预告。")
    
    print(f"--> 生成普通 xml: {OUTPUT_XML}")
    tree.write(OUTPUT_XML, encoding="utf-8", xml_declaration=True)
    
    print(f"--> 生成 gzip xml: {OUTPUT_GZ}")
    with open(OUTPUT_XML, "rb") as f_in:
        with gzip.open(OUTPUT_GZ, "wb", compresslevel=9) as f_out:
            f_out.writelines(f_in)

    print("[*] 全部完成！")

if __name__ == "__main__":
    process()
