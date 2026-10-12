import copy
import gzip
import io
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ==================== 配置区 ====================
DEFAULT_SOURCES = [
    "https://raw.githubusercontent.com/kuke31/xmlgz/main/e.xml.gz",
    "https://myepg.org/EPG/Zhejiang_Multicast.xml.gz",
    "https://gitee.com/mytv-android/myepg/raw/master/output/epg.gz",
    "https://epg.zsdc.eu.org/t.xml.gz",
    "http://epg.51zmt.top:8000/e1.xml.gz",
    "https://gitee.com/taksssss/tv/raw/main/epg/112114.xml.gz",
]

def parse_env_list(env_key, default=None):
    """从环境变量解析列表，支持换行、逗号或分号，过滤空行和注释(#)"""
    val = os.getenv(env_key, "").strip()
    if not val:
        return default if default is not None else []
    lines = re.split(r'[\r\n,;]+', val)
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]

# 1. 上游源地址列表（优先从环境变量读取，默认内置稳定多源）
SOURCES = parse_env_list("EPG_SOURCES", default=DEFAULT_SOURCES)

# 2. 本地 m3u 文件路径
M3U_PATH = os.getenv("M3U_PATH", "tv.m3u")

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "dist")
OUTPUT_XML = os.path.join(OUTPUT_DIR, "epg.xml")
OUTPUT_GZ = os.path.join(OUTPUT_DIR, "epg.xml.gz")
# ===============================================

def normalize_text(s):
    """标准化频道名称/ID，去除清晰度标签、特殊符号并转小写"""
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"(?i)\s*(?:4k|8k|fhd|hd|hevc|1080p|720p)\s*", "", s)
    s = s.replace("＋", "+")
    s = re.sub(r"[\s\-_]+", "", s).lower()
    return s

def load_channels_from_m3u(file_path):
    """
    解析本地 m3u 文件，提取频道信息并构建别名索引映射表。
    返回:
      m3u_channels: 目标频道列表 [{canonical_id, canonical_name, ...}, ...]
      alias_map: normalized_token -> canonical_id 的字典映射
    """
    m3u_channels = []
    alias_map = {}

    if not os.path.exists(file_path):
        print(f" [提示] 未找到 {file_path} 文件。")
        return m3u_channels, alias_map

    print(f"--> 正在从 {file_path} 读取频道列表...")
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("#EXTINF"):
                continue

            # 1. 提取 tvg-id="..."
            tvg_id_m = re.search(r'tvg-id="([^"]+)"', line, re.IGNORECASE)
            tvg_id = tvg_id_m.group(1).strip() if tvg_id_m else ""

            # 2. 提取 tvg-name="..."
            tvg_name_m = re.search(r'tvg-name="([^"]+)"', line, re.IGNORECASE)
            tvg_name = tvg_name_m.group(1).strip() if tvg_name_m else ""

            # 3. 提取末尾的频道名称 (逗号后面的名称)
            name_part = line.split(",")[-1].strip()
            if name_part.startswith("#"):
                name_part = ""

            canonical_id = tvg_id or tvg_name or name_part
            canonical_name = tvg_name or name_part or tvg_id

            if not canonical_id:
                continue

            channel_info = {
                "canonical_id": canonical_id,
                "canonical_name": canonical_name,
                "tvg_id": tvg_id,
                "tvg_name": tvg_name,
                "title": name_part
            }
            m3u_channels.append(channel_info)

            # 构建智能别名索引
            tokens = {tvg_id, tvg_name, name_part}
            norm_keys = set()
            for k in tokens:
                if k:
                    norm_keys.add(normalize_text(k))
                    norm_keys.add(k.strip().lower())

            # CCTV 系列智能匹配（兼容 cctv1、cctv-1、cctv 1 等）
            m_cctv = re.match(r"(?i)^cctv(\d+\+?)", canonical_id)
            if m_cctv:
                num = m_cctv.group(1)
                norm_keys.add(f"cctv{num}".lower())

            # 影视剧场 / NewTV / iHOT 前缀智能兼容
            cid_norm = normalize_text(canonical_id)
            norm_keys.add(f"newtv{cid_norm}")
            norm_keys.add(f"ihot{cid_norm}")
            norm_keys.add(f"ihot爱{cid_norm}")

            clean_prefix = re.sub(r"(?i)^(?:ihot|newtv)爱?", "", cid_norm)
            if clean_prefix:
                norm_keys.add(clean_prefix)
                norm_keys.add(f"爱{clean_prefix}")
                norm_keys.add(f"newtv{clean_prefix}")
                norm_keys.add(f"newtv爱{clean_prefix}")

            # 常见地方台名称微差异兼容
            if "浙江新闻" in canonical_id:
                norm_keys.add(normalize_text("浙江公共新闻"))
                norm_keys.add(normalize_text("公共新闻"))
            if "钱江都市" in canonical_id:
                norm_keys.add(normalize_text("钱江频道"))
                norm_keys.add(normalize_text("浙江钱江都市"))
            if "西湖明珠" in canonical_id:
                norm_keys.add(normalize_text("杭州2明珠"))
                norm_keys.add(normalize_text("明珠频道"))
                norm_keys.add(normalize_text("杭州明珠"))
            if "杭州青少体育" in canonical_id:
                norm_keys.add(normalize_text("杭州5青少"))
                norm_keys.add(normalize_text("杭州青少"))

            for nk in norm_keys:
                if nk:
                    alias_map[nk] = canonical_id

    print(f"[*] 成功从 {file_path} 提取出 {len(m3u_channels)} 个频道，构建了 {len(alias_map)} 个别名匹配项。")
    return m3u_channels, alias_map

def match_channel(tokens, alias_map):
    """通过 tokens 匹配到 m3u 中的规范频道 ID"""
    for tok in tokens:
        if not tok:
            continue
        ntok = normalize_text(tok)
        if ntok in alias_map:
            return alias_map[ntok]

        # 优先匹配带 + 号的 CCTV（如 cctv5+）
        m_cctv_plus = re.match(r"^cctv(\d+)\+", ntok)
        if m_cctv_plus:
            cctv_key = f"cctv{m_cctv_plus.group(1)}+"
            if cctv_key in alias_map:
                return alias_map[cctv_key]

        # 普通 CCTV 匹配
        m_cctv = re.match(r"^cctv(\d+)", ntok)
        if m_cctv and "+" not in ntok:
            cctv_key = f"cctv{m_cctv.group(1)}"
            if cctv_key in alias_map:
                return alias_map[cctv_key]

    return None

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

    # 1. 从根目录 tv.m3u 加载目标频道与别名映射
    m3u_channels, alias_map = load_channels_from_m3u(M3U_PATH)

    # 计算北京时间（UTC+8）当前日期，用于过滤只保留当日及后续节目
    cst_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(cst_tz).strftime("%Y%m%d")
    print(f"[*] 节目预告日期过滤基准 (北京时间当日): {today_str}")

    root = ET.Element("tv", {
        "generator-info-name": "GitHub-Actions-EPG-Tool",
        "date": datetime.now(cst_tz).strftime("%Y%m%d%H%M%S")
    })

    # 先初始化写入 tv.m3u 中的规范频道节点，确保每个频道都有标准的 id 与 display-name
    for ch in m3u_channels:
        c_elem = ET.SubElement(root, "channel", {"id": ch["canonical_id"]})
        dn_elem = ET.SubElement(c_elem, "display-name", {"lang": "zh"})
        dn_elem.text = ch["canonical_name"]

    seen_programmes = set()      # 节目 (canonical_id, start, title) 去重
    matched_canonical_channels = set()
    channel_prog_count = {ch["canonical_id"]: 0 for ch in m3u_channels}

    print(f"[*] 当前配置的上游源数量: {len(SOURCES)}")

    # 2. 遍历下载并解析上游 EPG XML
    for url in SOURCES:
        content = fetch_content(url)
        if not content:
            continue

        src_id_to_canonical = {}
        try:
            context = ET.iterparse(io.BytesIO(content), events=("end",))
            for event, elem in context:
                if elem.tag == "channel":
                    c_id = elem.get("id") or ""
                    names = [dn.text.strip() for dn in elem.findall("display-name") if dn.text]
                    matched_canonical = match_channel([c_id] + names, alias_map)
                    if matched_canonical and c_id:
                        src_id_to_canonical[c_id] = matched_canonical
                        matched_canonical_channels.add(matched_canonical)
                    elem.clear()

                elif elem.tag == "programme":
                    upstream_cid = elem.get("channel")
                    canonical_cid = src_id_to_canonical.get(upstream_cid)

                    # 只要上游频道映射到了本地 m3u 目标频道，就统一将 channel 重写为本地 canonical_id
                    if canonical_cid:
                        start = elem.get("start") or ""
                        stop = elem.get("stop") or ""
                        # 过滤掉过往节目：如果节目的结束时间（或开始时间）早于今天 00:00:00，则跳过
                        prog_end = stop[:8] if len(stop) >= 8 else (start[:8] if len(start) >= 8 else "")
                        if prog_end and prog_end < today_str:
                            elem.clear()
                            continue

                        title_elem = elem.find("title")
                        title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""

                        prog_key = (canonical_cid, start, title)
                        if prog_key not in seen_programmes:
                            seen_programmes.add(prog_key)
                            # 必须 deepcopy，否则后续 elem.clear() 会将已加入 root 的节点内容清空造成 <programme />
                            prog_node = copy.deepcopy(elem)
                            prog_node.set("channel", canonical_cid)
                            root.append(prog_node)
                            channel_prog_count[canonical_cid] += 1
                    elem.clear()

        except Exception as e:
            print(f" [错误] 解析 XML 失败 ({url}): {e}")

    # 3. 统计并导出生成结果
    covered_channels = sum(1 for cnt in channel_prog_count.values() if cnt > 0)
    print(f"\n[*] 汇总完成: m3u 共有 {len(m3u_channels)} 个频道，成功覆盖 {covered_channels} 个频道的节目单，共 {len(seen_programmes)} 条节目预告。")

    if len(m3u_channels) > covered_channels:
        missing = [cid for cid, cnt in channel_prog_count.items() if cnt == 0]
        print(f" [提示] 仍未匹配到节目的频道 ({len(missing)}): {', '.join(missing)}")

    print(f"--> 生成普通 xml: {OUTPUT_XML}")
    tree = ET.ElementTree(root)
    tree.write(OUTPUT_XML, encoding="utf-8", xml_declaration=True)

    print(f"--> 生成 gzip xml: {OUTPUT_GZ}")
    with open(OUTPUT_XML, "rb") as f_in:
        with gzip.open(OUTPUT_GZ, "wb", compresslevel=9) as f_out:
            f_out.writelines(f_in)

    print("[*] 全部完成！")

if __name__ == "__main__":
    process()
