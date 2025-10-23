# sync_music_downloads.py
import requests
import json
import time
import sys
import os
import re
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple

# --- 全局配置 ---
# 从 GitHub Actions 环境变量中获取 MCP 服务的 URL
MCP_SERVICE_BASE_URL = os.getenv("MCP_SERVICE_URL")
if not MCP_SERVICE_BASE_URL:
    print("❌ 错误: 缺少 MCP_SERVICE_URL 环境变量。请在 GitHub Secrets 中配置它。")
    sys.exit(1)
MCP_API_ENDPOINT = f"{MCP_SERVICE_BASE_URL}/mcp"

# 从 GitHub Actions 环境变量中获取外部音乐 API 的 URL
VKEYS_BASE_URL = os.getenv("VKEYS_BASE_URL", "https://api.vkeys.cn/v2/music/tencent")

DOWNLOAD_DIR = Path("downloads")

# --- API 常量和重试配置 ---
MAX_RETRIES = 3
INITIAL_REQUEST_DELAY = 2.0
RETRY_DELAY_MULTIPLIER = 2
API_TIMEOUT = 30


def print_status(message, end='\n'):
    """统一的打印函数，方便管理输出并确保立即显示。"""
    print(f"[STATUS] {message}", end=end)
    sys.stdout.flush()


def call_mcp_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any] | None:
    """调用 FastMCP 服务的 JSON-RPC 工具。"""
    payload = {
        "tool_name": tool_name,
        "args": args
    }
    headers = {"Content-Type": "application/json"}

    for attempt in range(MAX_RETRIES + 1):
        try:
            print_status(f"调用 MCP-DB 工具 '{tool_name}' (尝试 {attempt + 1}/{MAX_RETRIES + 1})...", end='\r')
            response = requests.post(MCP_API_ENDPOINT, json=payload, headers=headers, timeout=API_TIMEOUT)
            response.raise_for_status()

            result = response.json()
            tool_output_str = result.get('output', '{}')
            tool_output = json.loads(tool_output_str)

            if tool_output.get('status') == 'success':
                print_status(f"✅ MCP-DB 工具 '{tool_name}' 调用成功。")
                return tool_output
            else:
                print_status(f"❌ MCP-DB 工具 '{tool_name}' 返回失败状态: {tool_output_str}")
                return tool_output
        except requests.exceptions.Timeout:
            print_status(f"❌ MCP-DB 工具 '{tool_name}' 请求超时。")
        except requests.exceptions.RequestException as e:
            print_status(f"❌ MCP-DB 工具 '{tool_name}' 请求错误: {e}")
        except json.JSONDecodeError:
            print_status(f"❌ MCP-DB 工具 '{tool_name}' 响应不是有效 JSON: {response.text}")
        except Exception as e:
            print_status(f"❌ MCP-DB 工具 '{tool_name}' 发生意外错误: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(INITIAL_REQUEST_DELAY * (RETRY_DELAY_MULTIPLIER ** attempt))
    print_status(f"❌ MCP-DB 工具 '{tool_name}' {MAX_RETRIES + 1} 次尝试后仍失败。")
    return {"status": "error", "message": "Failed after multiple retries."}


def vkeys_api_request(url: str) -> Dict[str, Any] | None:
    """通用的 vkeys API 请求函数，包含重试逻辑。"""
    for attempt in range(MAX_RETRIES + 1):
        try:
            print_status(f"调用 VKEYS_API (attempt {attempt + 1}): {url}", end='\r')
            response = requests.get(url, timeout=API_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if data.get("code") == 200 and data.get("data"):
                return data["data"]
            else:
                print_status(f"⚠️ VKEYS_API 响应非成功状态码或无数据。响应: {data}")
                return None
        except requests.exceptions.Timeout:
            print_status(f"❌ VKEYS_API 请求超时。")
        except requests.exceptions.RequestException as e:
            print_status(f"❌ VKEYS_API 请求错误: {e}")
        except json.JSONDecodeError:
            print_status(f"❌ VKEYS_API 响应不是有效 JSON: {response.text}")
        except Exception as e:
            print_status(f"❌ VKEYS_API 发生意外错误: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(INITIAL_REQUEST_DELAY * (RETRY_DELAY_MULTIPLIER ** attempt))
    print_status(f"❌ VKEYS_API 在 {MAX_RETRIES + 1} 次尝试后仍失败。")
    return None


def sanitize_filename(filename: str) -> str:
    """清理文件名，移除或替换无效字符，确保跨平台兼容性。"""
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    return filename[:200]


def download_streaming_file(url: str, target_path: Path, retries=MAX_RETRIES) -> bool:
    """使用流式下载文件，包含重试和错误处理。"""
    if target_path.exists():
        if target_path.stat().st_size < 1024:
            print_status(f"文件 {target_path.name} 已存在但大小异常，尝试重新下载。")
        else:
            print_status(f"文件已存在，跳过下载: {target_path.name}")
            return True

    print_status(f"开始下载 {target_path.name}...")
    for attempt in range(retries + 1):
        try:
            with requests.get(url, stream=True, timeout=API_TIMEOUT) as r:
                r.raise_for_status()
                with open(target_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                print_status(f"下载成功: {target_path.name}")
                return True
        except requests.exceptions.RequestException as e:
            print_status(f"下载请求错误 (尝试 {attempt + 1}/{retries + 1}): {e}")
        except IOError as e:
            print_status(f"文件写入错误 {target_path}: {e}")
            return False

        if attempt < retries:
            time.sleep(INITIAL_REQUEST_DELAY * (RETRY_DELAY_MULTIPLIER ** attempt))
    print_status(f"❌ 文件 {target_path.name} 在 {retries + 1} 次尝试后下载失败。")
    return False


def save_lyric_file(content: str, target_path: Path) -> bool:
    """保存歌词文件。"""
    if not content or not content.strip():
        return True  # 无内容则认为成功，无需创建文件

    try:
        if target_path.exists():
            current_content = target_path.read_text(encoding='utf-8')
            if current_content.strip() == content.strip():
                print_status(f"歌词文件已存在且内容一致，跳过保存: {target_path.name}")
                return True

        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print_status(f"歌词文件保存成功: {target_path.name}")
        return True
    except IOError as e:
        print_status(f"歌词文件写入失败 ({target_path.name}): {e}")
        return False


def process_single_song_download(artist: str, song_name: str, expected_files: Set[Path]) -> bool:
    """
    处理单首歌曲的文件下载 (音乐和歌词)，并将成功生成的文件路径添加到 expected_files 集合中。
    从数据库获取的 artist, song_name 作为输入。
    """
    normalized_query = f"{artist} {song_name}".replace('-', ' ').strip()
    print_status(f"\n--- 处理文件下载: {artist} - {song_name} ---")

    search_api = f"{VKEYS_BASE_URL}?word={urllib.parse.quote(normalized_query)}"
    search_data = vkeys_api_request(search_api)

    if not search_data:
        print_status(f"❌ 搜索 '{artist} - {song_name}' 失败或无结果。")
        return False

    song_info = search_data[0]  # 取第一个结果
    actual_title, actual_artist, song_id = song_info['song'], song_info['singer'], song_info['id']
    print_status(f"找到最匹配结果: {actual_title} - {actual_artist} (ID: {song_id})")

    filename_prefix = sanitize_filename(f"{actual_title} - {actual_artist}")

    details_api = f"{VKEYS_BASE_URL}/geturl?id={song_id}"
    details = vkeys_api_request(details_api)
    if not details or not details.get('url'):
        print_status(f"❌ 无法获取歌曲 '{actual_title}' 的下载 URL 或格式。")
        return False

    music_format = details.get('format', 'mp3')
    music_file_path = DOWNLOAD_DIR / f"{filename_prefix}.{music_format}"

    lyric_api = f"{VKEYS_BASE_URL}/lyric?id={song_id}"
    lyrics_data = vkeys_api_request(lyric_api)
    lrc_content = lyrics_data.get('lrc', '') if lyrics_data else ''
    trans_content = lyrics_data.get('trans', '') if lyrics_data else ''
    lrc_file_path = DOWNLOAD_DIR / f"{filename_prefix}.lrc"
    trans_file_path = DOWNLOAD_DIR / f"{filename_prefix}.trans.txt"

    success = True
    if download_streaming_file(details['url'], music_file_path):
        expected_files.add(music_file_path)
    else:
        success = False

    if lrc_content:
        if save_lyric_file(lrc_content, lrc_file_path):
            expected_files.add(lrc_file_path)
        else:
            success = False

    if trans_content:
        if save_lyric_file(trans_content, trans_file_path):
            expected_files.add(trans_file_path)
        else:
            success = False

    return success


def sync_physical_downloads_directory(expected_files: Set[Path]):
    """将 downloads 目录与期望的文件列表同步，删除多余的文件。"""
    print_status("\n" + "=" * 60)
    print_status("--- 阶段 C: 同步下载目录，清理旧文件 ---")

    if not DOWNLOAD_DIR.exists():
        print_status("下载目录不存在，无需清理。")
        return

    actual_files = {p for p in DOWNLOAD_DIR.rglob('*') if p.is_file()}
    files_to_delete = actual_files - expected_files

    if not files_to_delete:
        print_status("下载目录已是最新状态，没有文件需要删除。")
        return

    for f in files_to_delete:
        try:
            f.unlink()
            print_status(f"  - 已删除: {f.relative_to(DOWNLOAD_DIR)}")
        except OSError as e:
            print_status(f"  - 删除失败: {f.relative_to(DOWNLOAD_DIR)} ({e})")
    print_status("下载目录清理完成。")


def main():
    print_status("--- 欢迎使用 GitHub Actions 音乐下载同步工作流 (数据库驱动) ---")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    print_status(f"\n" + "=" * 60)
    print_status(f"--- 阶段 A: 从 Vercel Postgres 数据库获取歌曲列表 ---")

    final_songs_for_download_response = call_mcp_tool("list_music_data", {"table_name": "songs", "limit": 10000})
    final_songs_for_download: List[Dict[str, Any]] = []

    if final_songs_for_download_response and final_songs_for_download_response.get('status') == 'success':
        final_songs_for_download = final_songs_for_download_response.get('data', [])
        print_status(f"数据库中最终有 {len(final_songs_for_download)} 首歌曲待处理文件下载。")
    else:
        print_status("❌ 无法从数据库获取最终歌曲列表进行文件下载。")
        sys.exit(1)

    print_status("\n" + "=" * 60)
    print_status("--- 阶段 B: 遍历歌曲列表，下载/更新文件 ---")

    expected_files_from_downloads: Set[Path] = set()

    if final_songs_for_download:
        for i, song_entry in enumerate(final_songs_for_download, 1):
            artist = song_entry.get('artist', '未知歌手')
            song_name = song_entry.get('song_name', '未知歌曲')
            print_status(f"\n--- 文件下载进度: ({i}/{len(final_songs_for_download)}) - {artist} - {song_name} ---")
            process_single_song_download(artist, song_name, expected_files_from_downloads)
            time.sleep(INITIAL_REQUEST_DELAY)
    else:
        print_status("数据库中没有歌曲，无需下载文件。")

    sync_physical_downloads_directory(expected_files_from_downloads)

    print_status("\n" + "=" * 60)
    print_status("--- 工作流执行完毕 ---")
    sys.exit(0)


if __name__ == "__main__":
    main()