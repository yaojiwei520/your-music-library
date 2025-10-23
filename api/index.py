# api/index.py
import sys
import io
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Literal

# === 引入 PostgreSQL 驱动 ===
import psycopg2
from psycopg2 import extras # 用于字典游标
import requests # 新增，用于发送 Webhook

# === 引入 FastMCP 框架 ===
from fastmcp import FastMCP

# =================================================================
# 核心优化 1：强制使用 UTF-8 编码 (主要用于本地调试)
# =================================================================
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
except Exception:
    pass

# =================================================================
# PostgreSQL 连接配置 (从环境变量读取)
# =================================================================
POSTGRES_URL = os.getenv("POSTGRES_URL")

# 强制检查关键环境变量是否设置
if not POSTGRES_URL:
    print("❌ 错误: 缺少必要的 POSTGRES_URL 环境变量。请将 Vercel Project 链接到 Vercel Postgres 数据库。")
    raise ValueError("Missing POSTGRES_URL environment variable. Please link a Vercel Postgres database.")

TABLE_SONGS = "songs"
TABLE_ARTISTS = "artists"

# =================================================================
# GitHub Actions Webhook 配置 (新增)
# =================================================================
# 这些变量需要在 Vercel 项目的环境变量中设置 (作为 Secrets)
# GITHUB_REPO_OWNER: GitHub 仓库所有者 (例如: your-username)
# GITHUB_REPO_NAME: GitHub 仓库名称 (例如: your-music-library)
# GITHUB_PERSONAL_ACCESS_TOKEN: 具有 'repo' 权限的 PAT，用于触发 GHA。请务必保密！
GITHUB_REPO_OWNER = os.getenv("GITHUB_REPO_OWNER")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO_NAME")
GITHUB_PERSONAL_ACCESS_TOKEN = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")

# 如果缺少这些变量，服务仍然可以启动，但无法触发 GitHub Actions
if not all([GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_PERSONAL_ACCESS_TOKEN]):
    print("⚠️ 警告: 缺少 GitHub Actions Webhook 必要的环境变量 (GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_PERSONAL_ACCESS_TOKEN)。")
    print("           FastMCP 服务将无法自动触发 GitHub Actions。")

# =================================================================
# 数据库辅助类 (psycopg2 异步连接和批量操作)
# =================================================================
class DatabaseManager:
    """管理 PostgreSQL 数据库连接和操作的类。"""

    def __init__(self, dsn: str):
        self.dsn = dsn

    @asynccontextmanager
    async def get_connection(self):
        """提供一个异步上下文管理器，用于每次操作时获取和自动关闭数据库连接。"""
        conn = None
        try:
            def _connect():
                nonlocal conn
                conn = psycopg2.connect(self.dsn)
                conn.autocommit = True
            await asyncio.to_thread(_connect)
            yield conn
        finally:
            if conn:
                def _close():
                    conn.close()
                await asyncio.to_thread(_close)

    async def execute_query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """异步执行查询 (SELECT)。"""
        async with self.get_connection() as conn:
            def _execute():
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cursor:
                    cursor.execute(sql, params)
                    results = cursor.fetchall()
                return results
            return await asyncio.to_thread(_execute)

    async def execute_non_query(self, sql: str, params: tuple = ()) -> int:
        """异步执行非查询操作 (INSERT, UPDATE, DELETE 单条)。"""
        async with self.get_connection() as conn:
            def _execute():
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                return cursor.rowcount
            return await asyncio.to_thread(_execute)

    async def execute_many(self, sql: str, params_list: List[tuple]) -> int:
        """异步执行批量非查询操作 (INSERT MANY, DELETE MANY)。"""
        async with self.get_connection() as conn:
            def _execute():
                with conn.cursor() as cursor:
                    cursor.executemany(sql, params_list)
                return len(params_list) # psycopg2.executemany doesn't return total rows affected directly
            return await asyncio.to_thread(_execute)

db_manager = DatabaseManager(POSTGRES_URL)

# =================================================================
# MCP Lifespan (数据库表创建和初始化数据)
# =================================================================
async def create_tables_if_not_exist():
    """使用 DatabaseManager 创建 songs 和 artists 表 (如果不存在)。"""
    sql_artists = f"""
    CREATE TABLE IF NOT EXISTS "{TABLE_ARTISTS}" (
        artist_id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        genre VARCHAR(50),
        country VARCHAR(50)
    );
    """
    await db_manager.execute_non_query(sql_artists)

    sql_songs = f"""
    CREATE TABLE IF NOT EXISTS "{TABLE_SONGS}" (
        id SERIAL PRIMARY KEY,
        artist VARCHAR(255) NOT NULL,
        song_name VARCHAR(255) NOT NULL
    );
    """
    await db_manager.execute_non_query(sql_songs)

    try:
        await db_manager.execute_non_query(
            f"INSERT INTO \"{TABLE_ARTISTS}\" (name, genre, country) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING;",
            ("The Beatles", "Rock", "UK")
        )
    except Exception as e:
        print(f"⚠️ 插入初始艺术家数据失败 (可能已存在或发生其他错误): {e}")

@asynccontextmanager
async def lifespan(app):
    """FastMCP 的生命周期管理。在 Vercel 每次冷启动或部署时会执行。"""
    print(f"🔄 Vercel function cold start/deployment: Checking/Creating PostgreSQL tables...")
    try:
        await create_tables_if_not_exist()
        print("✅ 数据库表结构和初始数据设置完成。")
    except Exception as e:
        print(f"❌ 数据库连接/初始化失败: {type(e).__name__}: {e}")
        raise # 抛出异常以阻止不健康的部署
    yield
    print("👋 Vercel function clean up (connections are per-request).")

# =================================================================
# 初始化 FastMCP 应用
# =================================================================
mcp_app = FastMCP(
    name="Music Database Service (Vercel Postgres)",
    instructions="This service provides full CRUD access to the music database via optimized HTTP/JSON-RPC tools, deployed on Vercel with Vercel Postgres, and can trigger GitHub Actions on database changes.",
    lifespan=lifespan
)

# =================================================================
# GitHub Actions Webhook 触发函数 (新增)
# =================================================================
def trigger_github_action_webhook(event_type: str, client_payload: Dict[str, Any]):
    """
    通过 GitHub Repository Dispatch Webhook 触发 GitHub Actions。
    https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#repository_dispatch
    """
    if not all([GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_PERSONAL_ACCESS_TOKEN]):
        print("⚠️ 无法触发 GitHub Actions Webhook，缺少必要的配置 (OWNER, NAME, PAT)。")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_PERSONAL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "event_type": event_type,
        "client_payload": client_payload
    }

    try:
        print(f"🔄 正在尝试触发 GitHub Actions Webhook (event_type: {event_type})...")
        response = requests.post(url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        print(f"✅ 成功触发 GitHub Actions Webhook (event_type: {event_type})。")
    except requests.exceptions.RequestException as e:
        print(f"❌ 触发 GitHub Actions Webhook 失败: {e}")
    except Exception as e:
        print(f"❌ 触发 GitHub Actions Webhook 时发生意外错误: {e}")

# =================================================================
# FastMCP Tools (修改 CRUD 工具，在成功修改后触发 Webhook)
# =================================================================

# --- 1. 查 (Read) ---
@mcp_app.tool()
async def list_music_data(
        table_name: Literal["artists", "songs"],
        limit: int = 10,
        offset: int = 0,
        filter_conditions: Optional[Dict[str, Any]] = None,
) -> str:
    """从指定音乐表中检索数据 (查)。"""
    if limit > 100: limit = 100
    if table_name not in [TABLE_ARTISTS, TABLE_SONGS]:
        return json.dumps({"status": "error", "message": "Invalid table name specified."}, ensure_ascii=False)

    sql = f"SELECT * FROM \"{table_name}\""
    params = []
    if filter_conditions:
        where_clauses = []
        for key, value in filter_conditions.items():
            where_clauses.append(f"\"{key}\" ILIKE %s")
            params.append(f"%{value}%")
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    try:
        results = await db_manager.execute_query(sql, tuple(params))
        return json.dumps({
            "status": "success",
            "count": len(results),
            "data": results
        }, ensure_ascii=False)
    except Exception as e:
        print(f"❌ PostgreSQL query failed for list_music_data: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL query failed: {e}"}, ensure_ascii=False)


# --- 2. 增 (Create) - 单条 ---
@mcp_app.tool()
async def add_song(
        song_name: str,
        artist: Optional[str] = None
) -> str:
    """向 'songs' 表中添加一首新歌曲 (增)，成功后触发 GitHub Actions。"""
    final_artist = artist if artist is not None and artist.strip() else "未知歌手"
    sql = f"INSERT INTO \"{TABLE_SONGS}\" (artist, song_name) VALUES (%s, %s)"
    params = (final_artist, song_name)
    try:
        row_count = await db_manager.execute_non_query(sql, params)
        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "add", "song_name": song_name, "artist": final_artist}
            )
            return json.dumps(
                {"status": "success", "message": f"Successfully added song: {song_name} by {final_artist}"},
                ensure_ascii=False)
        else:
            return json.dumps({"status": "error", "message": "Song not added."}, ensure_ascii=False)
    except Exception as e:
        print(f"❌ PostgreSQL insertion failed for add_song: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL insertion failed: {e}"}, ensure_ascii=False)


# --- 2. 增 (Create) - 批量 ---
@mcp_app.tool()
async def batch_add_songs(
        songs_list: List[Dict[str, str]],
        default_artist: Optional[str] = None
) -> str:
    """向 'songs' 表中批量添加多首歌曲 (增)，成功后触发 GitHub Actions。"""
    if not songs_list:
        return json.dumps({"status": "warning", "message": "The songs_list is empty."}, ensure_ascii=False)

    params_list = []
    final_default_artist = default_artist if default_artist is not None and default_artist.strip() else "未知歌手"

    for song in songs_list:
        song_name = song.get("song_name")
        artist = song.get("artist")
        if not song_name: continue
        final_artist = artist if artist is not None and artist.strip() else final_default_artist
        params_list.append((final_artist, song_name))

    if not params_list:
        return json.dumps({"status": "error", "message": "No valid songs found in the list."}, ensure_ascii=False)

    sql = f"INSERT INTO \"{TABLE_SONGS}\" (artist, song_name) VALUES (%s, %s)"

    try:
        row_count = await db_manager.execute_many(sql, params_list)
        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "batch_add", "count": row_count}
            )
        return json.dumps({
            "status": "success",
            "message": f"Successfully added approximately {row_count} songs.",
            "attempted_count": len(songs_list),
        }, ensure_ascii=False)

    except Exception as e:
        print(f"❌ PostgreSQL batch insertion failed for batch_add_songs: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL batch insertion failed: {e}"}, ensure_ascii=False)


# --- 3. 改 (Update) ---
@mcp_app.tool()
async def update_song(
        song_id: int,
        new_artist: Optional[str] = None,
        new_song_name: Optional[str] = None
) -> str:
    """根据歌曲ID修改歌曲的艺术家或名称 (改)，成功后触发 GitHub Actions。"""
    updates = []
    params = []

    if new_artist:
        updates.append("\"artist\" = %s")
        params.append(new_artist)
    if new_song_name:
        updates.append("\"song_name\" = %s")
        params.append(new_song_name)

    if not updates:
        return json.dumps({"status": "warning", "message": "No fields provided for update."}, ensure_ascii=False)

    sql = f"UPDATE \"{TABLE_SONGS}\" SET " + ", ".join(updates) + " WHERE \"id\" = %s"
    params.append(song_id)

    try:
        row_count = await db_manager.execute_non_query(sql, tuple(params))
        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "update", "song_id": song_id}
            )
            return json.dumps({"status": "success", "message": f"Successfully updated song with ID: {song_id}."},
                              ensure_ascii=False)
        else:
            return json.dumps({"status": "warning", "message": f"No song found with ID: {song_id}."},
                              ensure_ascii=False)
    except Exception as e:
        print(f"❌ PostgreSQL update failed for update_song: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL update failed: {e}"}, ensure_ascii=False)


# --- 4. 删 (Delete) - 单条 ---
@mcp_app.tool()
async def delete_song(
        song_name: str,
        artist: Optional[str] = None
) -> str:
    """从 'songs' 表中删除一首指定名称的歌曲 (删)，成功后触发 GitHub Actions。"""
    sql = f"DELETE FROM \"{TABLE_SONGS}\" WHERE \"song_name\" = %s"
    params = [song_name]

    if artist:
        sql += " AND \"artist\" = %s"
        params.append(artist)

    try:
        row_count = await db_manager.execute_non_query(sql, tuple(params))
        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "delete", "song_name": song_name, "artist": artist, "count": row_count}
            )
            return json.dumps(
                {"status": "success", "message": f"Successfully deleted {row_count} song(s) named: {song_name}."},
                ensure_ascii=False)
        else:
            return json.dumps(
                {"status": "warning", "message": f"No song found named: {song_name} (or artist mismatch)."},
                ensure_ascii=False)
    except Exception as e:
        print(f"❌ PostgreSQL deletion failed for delete_song: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL deletion failed: {e}"}, ensure_ascii=False)


# --- 4. 删 (Delete) - 批量 ---
@mcp_app.tool()
async def batch_delete_songs(
        songs_list: List[Dict[str, str]],
        default_artist: Optional[str] = None
) -> str:
    """从 'songs' 表中批量删除多首歌曲 (删)，成功后触发 GitHub Actions。"""
    if not songs_list:
        return json.dumps({"status": "warning", "message": "The songs_list is empty."}, ensure_ascii=False)

    final_params_list = []
    final_default_artist = default_artist if default_artist is not None and default_artist.strip() else "未知歌手"

    for song in songs_list:
        song_name = song.get("song_name")
        artist = song.get("artist")
        if not song_name: continue
        final_artist = artist if artist is not None and artist.strip() else final_default_artist
        final_params_list.append((song_name, final_artist))

    if not final_params_list:
        return json.dumps({"status": "error", "message": "No valid songs found in the list."}, ensure_ascii=False)

    sql = f"DELETE FROM \"{TABLE_SONGS}\" WHERE \"song_name\" = %s AND \"artist\" = %s"

    try:
        row_count = await db_manager.execute_many(sql, final_params_list)

        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "batch_delete", "count": row_count}
            )
            return json.dumps({
                "status": "success",
                "message": f"成功删除约 {row_count} 首歌曲。",
                "deleted_count": row_count
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "status": "warning",
                "message": f"尝试删除 {len(final_params_list)} 首歌曲，但未找到匹配项 (0 首歌删除成功)。请检查歌手名称是否正确。",
                "deleted_count": 0
            }, ensure_ascii=False)

    except Exception as e:
        print(f"❌ PostgreSQL batch deletion failed for batch_delete_songs: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL batch deletion failed: {e}"}, ensure_ascii=False)


# --- 4. 删 (Delete) - 按歌手删除所有歌曲 ---
@mcp_app.tool()
async def delete_songs_by_artist(
        artist: str
) -> str:
    """从 'songs' 表中删除某位歌手的所有歌曲，成功后触发 GitHub Actions。"""
    if not artist or not artist.strip():
        return json.dumps({"status": "error", "message": "Artist name is required."}, ensure_ascii=False)

    sql = f"DELETE FROM \"{TABLE_SONGS}\" WHERE \"artist\" = %s"
    params = (artist,)

    try:
        row_count = await db_manager.execute_non_query(sql, params)
        if row_count > 0:
            trigger_github_action_webhook(
                event_type="db_music_updated",
                client_payload={"action": "delete_by_artist", "artist": artist, "count": row_count}
            )
            return json.dumps({"status": "success", "message": f"成功删除歌手 {artist} 的 {row_count} 首歌曲。"},
                              ensure_ascii=False)
        else:
            return json.dumps({"status": "warning", "message": f"未找到歌手 {artist} 的任何歌曲，0 首歌被删除。"},
                              ensure_ascii=False)
    except Exception as e:
        print(f"❌ PostgreSQL deletion failed for delete_songs_by_artist: {e}")
        return json.dumps({"status": "error", "message": f"PostgreSQL deletion failed: {e}"}, ensure_ascii=False)


# --- 辅助工具 (查结构) ---
@mcp_app.tool()
async def get_table_structure(
        table_name: Literal["artists", "songs"],
) -> str:
    """获取指定音乐表的列结构（名称和类型）。"""
    if table_name not in [TABLE_ARTISTS, TABLE_SONGS]:
        return json.dumps({"status": "error", "message": "Invalid table name specified."}, ensure_ascii=False)

    sql = f"""
    SELECT column_name AS Field, udt_name AS Type, is_nullable AS Null
    FROM information_schema.columns
    WHERE table_name = %s AND table_schema = current_schema();
    """
    try:
        results = await db_manager.execute_query(sql, (table_name,))
        structure = [
            {"name": row['field'], "type": row['type'], "notnull": 1 if row['null'] == 'NO' else 0}
            for row in results
        ]

        return json.dumps({
            "status": "success",
            "table": table_name,
            "structure": structure
        }, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Failed to get structure for {table_name}: {e}")
        return json.dumps({"status": "error", "message": f"Failed to get structure: {e}"}, ensure_ascii=False)

# =================================================================
# Vercel 需要直接暴露 ASGI 应用对象
# =================================================================
# mcp_app 已在上方定义