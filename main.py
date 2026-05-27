import urllib.request
from urllib.parse import urlparse, quote, parse_qs
import re
import os
from datetime import datetime, timedelta, timezone
import opencc
import ssl
import sys
import socket
import time
import concurrent.futures
from typing import List, Dict, Set, Tuple

# 跳过SSL证书验证
ssl._create_default_https_context = ssl._create_unverified_context

# 执行开始时间
timestart = datetime.now()

# 设置标准输出和错误输出立即刷新
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', 1)

print("脚本开始执行")

# 读取文本方法
def read_txt_to_array(file_name: str) -> List[str]:
    encodings = ['utf-8-sig', 'gbk', 'latin-1']
    for encoding in encodings:
        try:
            with open(file_name, 'r', encoding=encoding) as file:
                return [line.strip() for line in file.readlines() if line.strip()]
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            print(f"文件不存在: {file_name}")
            return []
    print(f"无法读取文件: {file_name}")
    return []

# 读取名单文件
def read_list_from_txt(file_path: str, is_blacklist: bool = True) -> List[str]:
    lines = read_txt_to_array(file_path)
    result = []
    for line in lines:
        if ',' in line:
            url = line.split(',')[1].strip()
            result.append(url)
        else:
            result.append(line)
    return result

# 读取频道字典
def load_channel_dictionaries() -> Dict[str, List[str]]:
    dictionaries = {}
    categories = {
        'zh': '主频道/综合频道.txt',
        'ys': '主频道/央视频道.txt',
        'ws': '主频道/卫视频道.txt',
        'dy': '主频道/电影.txt',
        'gj': '主频道/国际台.txt',
        'zb': '主频道/直播中国.txt',
        'hb': '地方台/湖北频道.txt'  # 修改为湖北频道，移除了 gd 和 hain
    }
    
    for key, path in categories.items():
        dictionaries[key] = read_txt_to_array(path)
    
    return dictionaries

# 简繁转换
def traditional_to_simplified(text: str) -> str:
    try:
        converter = opencc.OpenCC('t2s')
        return converter.convert(text)
    except Exception as e:
        print(f"简繁转换出错: {e}")
        return text

# M3U格式判断和转换
def process_m3u_content(text: str) -> str:
    if not text.strip().startswith("#EXTM3U"):
        return text
    
    lines = text.split('\n')
    result = []
    channel_name = ""
    
    for line in lines:
        if line.startswith("#EXTINF"):
            channel_name = line.split(',')[-1].strip()
        elif line.startswith(("http", "rtmp", "p3p")):
            result.append(f"{channel_name},{line.strip()}")
        elif "#genre#" not in line and "," in line and "://" in line:
            result.append(line)
    
    return '\n'.join(result)

# 清理频道名称
removal_list = ["「IPV4」", "「IPV6」", "[ipv6]", "[ipv4]", "_电信", "电信", "（HD）", "[超清]", "高清", "超清", "-HD", "(HK)", "AKtv", "@", "IPV6", "🎞🎞🎞🎞🎞🎞🎞🎞️", "🎦🎦🎦🎦🎦🎦🎦🎦", " ", "[BD]", "[VGA]", "[HD]", "[SD]", "(1080p)", "(720p)", "(480p)"]

def clean_channel_name(channel_name: str) -> str:
    for item in removal_list:
        channel_name = channel_name.replace(item, "")
    replacements = {
        "CCTV0": "CCTV-",
        "PLUS": "+",
        "NewTV-": "NewTV",
        "iHOT-": "iHOT",
        "NEW": "New",
        "New_": "New"
    }
    for old, new in replacements.items():
        channel_name = channel_name.replace(old, new)
    return channel_name

# 生成频道ID（用于EPG和LOGO）
def generate_channel_id(channel_name: str) -> str:
    # 处理CCTV频道
    if channel_name.startswith('CCTV'):
        match = re.search(r'CCTV[-\s]*(\d+)', channel_name)
        if match:
            return f"CCTV{match.group(1)}"
    
    # 处理其他频道
    # 移除特殊字符和空格
    channel_id = re.sub(r'[^\w]', '', channel_name)
    return channel_id

# 直播源验证函数
def validate_stream_url(url: str, timeout: int = 3) -> Tuple[bool, float]:
    try:
        start_time = time.time()
        
        parsed_url = urlparse(url)
        host = parsed_url.hostname
        port = parsed_url.port or (443 if parsed_url.scheme == 'https' else 80)
        
        # TCP连接测试
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        
        if result != 0:
            return False, None
            
        # HTTP/HTTPS验证
        if url.startswith(('http://', 'https://')):
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Connection': 'close',
                'Range': 'bytes=0-1024'
            }
            
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.getcode() not in [200, 206]:
                    return False, None
                
                content_type = response.headers.get('Content-Type', '')
                if not any(x in content_type for x in ['video', 'audio', 'application/octet-stream', 'application/vnd.apple.mpegurl']):
                    return False, None
                    
        end_time = time.time()
        return True, end_time - start_time
        
    except Exception as e:
        return False, None

# 频道源管理器
class ChannelSourceManager:
    def __init__(self, blacklist: Set[str] = None):
        self.sources = {}
        self.seen_urls = set()
        self.blacklist = blacklist if blacklist else set()
        
    def add_source(self, channel_name: str, url: str, skip_validation: bool = False) -> bool:
        if url in self.seen_urls:
            return False
            
        # 黑名单检查（模糊匹配：只要直播源URL包含黑名单中的任意一项，就进行拦截）
        if any(black_item in url for black_item in self.blacklist if black_item.strip()):
            return False
            
        self.seen_urls.add(url)
        
        if channel_name not in self.sources:
            self.sources[channel_name] = []
        
        if skip_validation:
            self.sources[channel_name].insert(0, (0, url))
        else:
            self.sources[channel_name].append((float('inf'), url))
        
        return True
        
    def validate_and_sort_sources(self, max_workers: int = 20) -> None:
        print("开始验证所有源的有效性...")
        
        all_urls = []
        url_to_channel = {}
        
        for channel_name, url_list in self.sources.items():
            for response_time, url in url_list:
                if response_time == float('inf'):
                    all_urls.append(url)
                    url_to_channel[url] = channel_name
        
        validated_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(validate_stream_url, url): url for url in all_urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    is_valid, response_time = future.result()
                    validated_results[url] = (is_valid, response_time)
                except Exception as e:
                    validated_results[url] = (False, None)
        
        for channel_name in list(self.sources.keys()):
            valid_sources = []
            for response_time, url in self.sources[channel_name]:
                if response_time == 0:
                    valid_sources.append((0, url))
                elif url in validated_results:
                    is_valid, actual_response_time = validated_results[url]
                    if is_valid and actual_response_time is not None:
                        valid_sources.append((actual_response_time, url))
            
            valid_sources.sort(key=lambda x: x[0])
            self.sources[channel_name] = valid_sources[:10]
    
    def get_sorted_lines(self, channel_dictionary: List[str], group_title: str) -> List[str]:
        result = []
        for channel_name in channel_dictionary:
            if channel_name in self.sources and self.sources[channel_name]:
                # 生成频道ID
                channel_id = generate_channel_id(channel_name)
                
                for response_time, url in self.sources[channel_name]:
                    # 创建M3U格式的行
                    extinf_line = f'#EXTINF:-1 tvg-name="{channel_id}" tvg-logo="https://11.112114.xyz/logo/{channel_id}.png" group-title="{group_title}",{channel_name}'
                    result.append(extinf_line)
                    result.append(url)
        return result

    def get_txt_lines(self, channel_dictionary: List[str]) -> List[str]:
        result = []
        for channel_name in channel_dictionary:
            if channel_name in self.sources and self.sources[channel_name]:
                for response_time, url in self.sources[channel_name]:
                    result.append(f"{channel_name},{url}")
        return result

# 处理频道行
def process_channel_line(line: str, source_manager: ChannelSourceManager, channel_dictionaries: Dict[str, List[str]], skip_validation: bool = False) -> None:
    try:
        if "#genre#" not in line and "#EXTINF:" not in line and "," in line and "://" in line:
            parts = line.split(',', 1)
            if len(parts) < 2:
                return
                
            channel_name, channel_address = parts
            original_name = channel_name  # 保存原始名称
            channel_name = traditional_to_simplified(channel_name)
            channel_name = clean_channel_name(channel_name)
            
            channel_address = channel_address.strip()
            
            # 检查是否为IPv6地址，如果是则跳过
            if re.search(r'\[[0-9a-fA-F:]+\]|ipv6|240[89e]:', channel_address, re.IGNORECASE):
                return

            # 分配到正确的频道分类
            for category, dictionary in channel_dictionaries.items():
                if channel_name in dictionary:
                    if source_manager.add_source(channel_name, channel_address, skip_validation):
                        print(f"添加到{category}: {channel_name}, {channel_address}")
                    return
            
            print(f"未分类频道: {channel_name}, {channel_address} (原始名称: {original_name})")
    except Exception as e:
        print(f"处理频道行时出错: {e}")

# 处理URL
def process_url(url: str, source_manager: ChannelSourceManager, channel_dictionaries: Dict[str, List[str]]) -> None:
    print(f"\n开始处理URL: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        req = urllib.request.Request(url, headers=headers)
        
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read()
            encodings = ['utf-8', 'gbk', 'iso-8859-1']
            text = None
            
            for encoding in encodings:
                try:
                    text = data.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            
            if text is None:
                print("无法确定合适的编码格式进行解码。")
                return

            text = process_m3u_content(text)
            lines = text.split('\n')
            
            for line in lines:
                if "#genre#" not in line and "," in line and "://" in line:
                    process_channel_line(line, source_manager, channel_dictionaries)
                    
    except Exception as e:
        print(f"处理URL时发生错误：{e}")

# 处理精选源文件
def process_me_file(source_manager: ChannelSourceManager, channel_dictionaries: Dict[str, List[str]]) -> None:
    print("\n开始处理精选源文件 me.txt...")
    me_lines = read_txt_to_array('assets/me.txt')
    
    for line in me_lines:
        if line.strip() and "," in line and "://" in line:
            process_channel_line(line, source_manager, channel_dictionaries, skip_validation=True)

# 主函数
def main():
    print("正在读取黑名单...")
    # 只读取blackhost_count.txt作为黑名单
    blacklist = read_list_from_txt('assets/whitelist-blacklist/blackhost_count.txt')
    print(f"黑名单行数: {len(blacklist)}")

    print("正在读取白名单...")
    whitelist = read_list_from_txt('assets/whitelist-blacklist/whitelist.txt', is_blacklist=False)
    print(f"白名单行数: {len(whitelist)}")

    print("正在读取频道字典...")
    channel_dictionaries = load_channel_dictionaries()

    print("正在读取URL列表...")
    urls = read_txt_to_array('assets/urls.txt')
    print(f"读取到 {len(urls)} 个URL")

    # 创建频道源管理器，传入黑名单
    source_manager = ChannelSourceManager(blacklist=set(blacklist))

    # 处理所有URL
    print("\n开始处理所有URL...")
    for url in urls:
        if url.startswith("http"):
            process_url(url, source_manager, channel_dictionaries)

    # 处理精选源文件
    process_me_file(source_manager, channel_dictionaries)

    # 验证所有源并选择最快的10个
    source_manager.validate_and_sort_sources()

    # 获取当前的 UTC 时间并格式化为带秒和横线的格式
    beijing_time = datetime.now(timezone.utc) + timedelta(hours=8)
    formatted_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")

    # 分类名称映射
    category_names = {
        'zh': "综合频道",
        'ys': "央视频道", 
        'ws': "卫视频道",
        'gj': "国际台",
        'hb': "湖北频道",  # 移除了 gd 和 hain，新增 hb
        'dy': "电影频道",
        'zb': "直播中国"
    }
    
    # 创建M3U文件内容（最上面加上免责与时间注释）
    all_m3u_lines = [
        "#EXTM3U",
        f"#{formatted_time}（北京时间）仅供娱乐，切勿商用。",
        "#PLAYLIST:电视直播",
        ""
    ]

    # 创建TXT文件内容（最上方不再放更新时间分组，初始化为空列表）
    all_txt_lines = []

    # 获取处理后的频道源并添加到M3U和TXT文件
    total_count = 0
    categories_order = ['ys', 'ws', 'zh', 'hb', 'dy']  # 调整后的排序顺序
    
    for category in categories_order:
        name = category_names[category]
        
        # 添加到M3U文件
        channel_m3u_lines = source_manager.get_sorted_lines(channel_dictionaries[category], name)
        count = len(channel_m3u_lines) // 2  # 每频道有两行：EXTINF和URL
        total_count += count
        print(f"{name}: {count} 个频道")
        
        # 添加分类标题到M3U
        all_m3u_lines.append(f"#========== {name} ==========#")
        all_m3u_lines.extend(channel_m3u_lines)
        all_m3u_lines.append('')
        
        # 添加到TXT文件
        all_txt_lines.append(f"{name},#genre#")
        channel_txt_lines = source_manager.get_txt_lines(channel_dictionaries[category])
        all_txt_lines.extend(channel_txt_lines)
        all_txt_lines.append('')

    # ==================== 将“更新时间”分组放到所有频道的最后下方 ====================
    
    # 追加到 M3U 文件末尾
    all_m3u_lines.append("#========== 更新时间 ==========#")
    all_m3u_lines.append(f'#EXTINF:-1 tvg-id="更新时间" tvg-name="更新时间" tvg-logo="" group-title="更新时间",更新时间: {formatted_time}（北京时间）仅供娱乐，切勿商用。')
    all_m3u_lines.append("https://jnsj.cloudplains.dpdns.org/tv202303.txt")
    all_m3u_lines.append('')

    # 追加到 TXT 文件末尾
    all_txt_lines.append("更新时间,#genre#")
    all_txt_lines.append(f"{formatted_time}（北京时间）仅供娱乐，切勿商用。,https://jnsj.cloudplains.dpdns.org/tv202303.txt")
    all_txt_lines.append('')

    # 保存M3U文件
    m3u_output_file = "tv202303.m3u"
    try:
        with open(m3u_output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_m3u_lines))
        print(f"M3U文件已保存到: {m3u_output_file}")
    except Exception as e:
        print(f"保存M3U文件时发生错误：{e}")

    # 保存TXT文件
    txt_output_file = "tv202303.txt"
    try:
        with open(txt_output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_txt_lines))
        print(f"TXT文件已保存到: {txt_output_file}")
    except Exception as e:
        print(f"保存TXT文件时发生错误：{e}")

    # 执行结束时间
    timeend = datetime.now()
    elapsed_time = timeend - timestart
    total_seconds = elapsed_time.total_seconds()
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)

    print(f"执行时间: {minutes} 分 {seconds} 秒")
    print(f"blacklist行数: {len(blacklist)}")
    print(f"{m3u_output_file}频道数: {total_count}")

if __name__ == "__main__":
    main()
