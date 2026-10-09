import gzip
import io
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# ==================== 从环境变量加载配置 ====================
def parse_env_list(env_key, default=None):
    """从环境变量解析列表，支持换行、逗号或分号分隔，并过滤空行和注释(#)"""
    val = os.getenv(env_key, "").strip()
    if not val:
        return default if default is not None else []
    lines = re.split(r'[\r\n,;]+', val)
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]

# 1. 上游源地址列表
SOURCES = parse_env_list("EPG_SOURCES", default=[
    # 默认兜底源，未配置环境变量时使用
    "http://epg.51zmt.top:8000/e.xml",
])

# 2. 目标频道白名单（支持频道名称或 ID）
# 若环境变量 TARGET_CHANNELS 未设置或为空，则代表不过滤频道，全量合并
TARGET_CHANNELS = set(parse_env_list("TARGET_CHANNELS", default=[]))

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "dist")
OUTPUT_XML = os.path.join(OUTPUT_DIR, "epg.xml")
OUTPUT_GZ = os.path.join(OUTPUT_DIR, "epg.xml.gz")
# ==========================================================

def fetch_content(url):
    print(f"--> 正在拉取: {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
            # 自动识别是否经过 gzip 压缩
            if url.endswith(".gz") or (len(data) > 2 and data[:2] == b'\x1f\x8b'):
                return gzip.decompress(data)
            return data
    except Exception as e:
        print(f" [警告] 下载失败 {url}: {e}")
        return None

def process():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    seen_channels = set()        # 频道 ID 去重
    seen_programmes = set()      # 节目 (channel, start, title) 去重
    matched_channel_ids = set()  # 记录匹配白名单的 channel id

    print(f"[*] 当前配置的上游源数量: {len(SOURCES)}")
    print(f"[*] 白名单频道数量: {'全部放行(未限制)' if not TARGET_CHANNELS else len(TARGET_CHANNELS)}")

    root = ET.Element("tv", {
        "generator-info-name": "GitHub-Actions-EPG-Tool",
        "date": datetime.now().strftime("%Y%m%d%H%M%S")
    })

    # 1. 遍历下载并流式解析
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
                    if not TARGET_CHANNELS:
                        is_match = True
                    else:
                        # 支持按 channel id 或 display-name 匹配
                        if c_id in TARGET_CHANNELS or any(n in TARGET_CHANNELS for n in names):
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

                    # 仅保留白名单频道的节目，且同一时段标题去重
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
    print(f"\n[*] 汇总完成: 共筛选出 {len(seen_channels)} 个频道，{len(seen_programmes)} 条节目预告。")
    
    print(f"--> 生成未压缩文件: {OUTPUT_XML}")
    tree.write(OUTPUT_XML, encoding="utf-8", xml_declaration=True)
    
    print(f"--> 生成 Gzip 压缩文件: {OUTPUT_GZ}")
    with open(OUTPUT_XML, "rb") as f_in:
        with gzip.open(OUTPUT_GZ, "wb", compresslevel=9) as f_out:
            f_out.writelines(f_in)

    print("[*] 全部完成！")

if __name__ == "__main__":
    process()
