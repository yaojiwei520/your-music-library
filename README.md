# My Automated Music Library

这个项目展示了一个无服务器的数据管理和自动化下载系统。

**如何工作：**

1.  **管理数据库：** 通过 FastMCP API（部署在 Vercel），你可以添加、删除、修改歌曲的元数据，这些数据存储在 Vercel Postgres 数据库中。
2.  **自动下载：** 当数据库中的歌曲列表发生变化时，FastMCP 应用会自动触发 GitHub Actions。
3.  **仓库同步：** GitHub Actions 工作流会从 Vercel Postgres 数据库获取最新的歌曲列表，然后使用第三方音乐 API 下载对应的音乐文件和歌词到 `downloads/` 目录，并自动提交这些更改到 GitHub 仓库。

## Getting Started

1.  ### Deploy FastMCP to Vercel
    Follow the instructions to deploy `api/index.py` your Vercel project with a linked Vercel Postgres database. Set the following environment variables (as secrets) in your Vercel project:
    *   `POSTGRES_URL` (automatically provided by Vercel Postgres)
    *   `GITHUB_REPO_OWNER` (e.g., `your-username`)
    *   `GITHUB_REPO_NAME` (e.g., `your-music-library`)
    *   `GITHUB_PERSONAL_ACCESS_TOKEN` (a PAT with `repo` permissions)

2.  ### Configure GitHub Actions
    Place `sync-downloads-from-db.yml` in `.github/workflows/` and `sync_music_downloads.py`, `requirements_sync_music.txt` in your repository root.
    Configure the following secrets in your GitHub repository:
    *   `MCP_SERVICE_URL` (the URL of your deployed Vercel FastMCP app)
    *   `VKEYS_BASE_URL` (e.g., `https://api.vkeys.cn/v2/music/tencent`)

3.  ### Interact with the Music Library
    Use an MCP client (like Cherry Studio) or custom scripts to interact with your Vercel FastMCP service (e.g., `https://your-vercel-app.vercel.app/mcp`) to add/delete/update songs.
    Any successful modification will automatically trigger the GitHub Actions workflow to sync your `downloads/` folder.

## `downloads/` Directory

This directory contains the music files and lyrics automatically downloaded and managed by the GitHub Actions workflow. **It's recommended not to manually modify content in this directory**, as changes might be overwritten by the automated sync.
