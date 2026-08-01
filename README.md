# M-AI Master

面向大型制造集团的 AI 主数据智能治理与智能共享系统。当前版本提供动态 CSV 字段识别、语义查重、黄金主数据归并、向量检索、图谱血缘、审核、跨工厂分发和反馈回流闭环。

## Windows 一键运行

1. 安装 Python 3.11 或更高版本，安装时勾选 `Add Python to PATH`。
2. 双击 `start.bat`。首次启动会在项目内创建独立的 `.venv` 并安装依赖。
3. 本机打开 `http://127.0.0.1:5000`；其他电脑打开启动窗口打印的 `LAN` 地址。

服务使用 Waitress 生产 WSGI，不再使用 Flask 开发服务器。启动窗口必须保持打开，按 `Ctrl+C` 停止。

## 换电脑或换 WiFi

- 复制整个项目目录，但不要复制 `.venv`；新电脑首次运行会重新创建适配本机的环境。
- 需要迁移业务数据时，先停止旧服务，再复制 `runtime/data/mdm_data.db`。兼容旧版本的数据库可能位于 `backend/mdm_data.db`。
- 更换 WiFi 后服务器 IP 可能变化，以新一次启动时打印的 `LAN` 地址为准。正式部署建议配置固定 IP、DHCP 地址保留或企业 DNS 域名。
- 其他设备无法访问时，以管理员 PowerShell 放行端口：

```powershell
New-NetFirewallRule -DisplayName "M-AI Master 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

前端始终使用相对 `/api` 接口，因此更换电脑、IP、域名或 WiFi 无需修改 HTML。

## 环境配置

复制 `backend/.env.example` 为 `backend/.env`。常用配置：

- `MDM_HOST=0.0.0.0`：允许局域网访问。
- `MDM_PORT=5000`：监听端口。
- `MDM_DB_PATH=../runtime/data/mdm_data.db`：持久化数据库。
- `MDM_LOG_DIR=../runtime/logs`：轮转日志目录。
- `MDM_AUTH_USER`、`MDM_AUTH_PASSWORD`：可选浏览器基础认证；设置密码后访问页面会要求登录。
- `DASHSCOPE_API_KEY`：启用通义 `text-embedding-v3`；缺失或网络失败会自动降级，不影响治理流程运行。

不要把包含真实密钥或密码的 `backend/.env` 上传到代码仓库。

## Docker 交付

适合在一台企业 Linux/Windows Docker 主机上集中部署：

```bash
docker compose up -d --build
docker compose ps
```

访问 `http://服务器IP:5000`，健康检查为 `http://服务器IP:5000/api/health`。数据库和日志分别持久化在 `runtime/data`、`runtime/logs`，升级镜像不会删除数据。

设置认证后再启动：

```powershell
$env:MDM_AUTH_USER="admin"
$env:MDM_AUTH_PASSWORD="请替换为强密码"
docker compose up -d --build
```

## 企业部署边界

当前 SQLite 架构适合单节点部门级试点或竞赛交付，Gunicorn 固定为一个进程并使用线程并发，避免多进程写入冲突。若要多服务器高可用、数百并发或跨地域部署，应把存储迁移到 PostgreSQL，并接入企业 SSO、HTTPS 反向代理、集中日志、密钥管理和定时备份；不能把开放端口的无密码 HTTP 服务直接暴露到互联网。

上线前至少完成：

- 设置 `MDM_AUTH_PASSWORD` 并通过 Nginx/企业网关启用 HTTPS。
- 固定服务器地址并限制防火墙来源网段。
- 定期备份数据库和验证恢复。
- 使用 `/api/health` 接入监控；`ready=true` 才表示数据库可用。
- 在预生产环境用真实字段和真实数据量完成验收。

## 主要文件

- `backend/app.py`：Flask API、治理引擎与闭环逻辑。
- `backend/index.html`：单页前端，接口使用同源相对路径。
- `backend/requirements.txt`：Python 依赖。
- `start.bat` / `start.sh`：Windows / Linux 生产启动。
- `backend/Dockerfile` / `docker-compose.yml`：容器交付。
- `test_api.py`：端到端回归测试。
