# M-AI Master 4.4

面向大型制造集团的 AI 物料主数据智能治理与共享演示系统。系统以 Flask + SQLite 为权威运行端，完成多源数据接入、标准识别、Embedding 向量入库、图谱辅助查重、多主体审核、质量评估、跨工厂分发和工厂反馈回流八步闭环。

## Windows 运行

建议安装 Python 3.11 x64，并在安装时启用 `Add Python to PATH`。

1. 普通稳定版：双击 `start.bat`。
2. 浏览器访问启动窗口显示的 `http://127.0.0.1:5000`；局域网设备使用窗口打印的 `LAN` 地址。
3. 在服务器电脑上首次点击“识别并标准化”时，系统会自动安装 Python 3.11、PaddleOCR CPU 依赖和模型，完成后自动继续识别。

自动安装只允许从运行 Flask 的服务器电脑触发，局域网其他电脑不能远程执行安装命令。OCR 被安装在独立 `.venv-ocr`，主平台即使使用 Python 3.14 也会通过隔离子进程调用 Python 3.11 OCR，无需重启。安装失败时会展示日志摘要，并自动尝试 Qwen-VL 或规则解析，治理流程不会崩溃。也可以手动双击 `install-ocr.bat`；脚本依次尝试阿里云、清华和官方 PyPI，并在真实导入 `paddle` 与 `paddleocr` 后才进入模型初始化。企业内网可在 `backend/.env` 中通过 `MDM_PIP_INDEX_URL` 指向内部 PyPI 代理。

## macOS / Linux 运行

macOS 或 Linux 安装 Python 3.11 及以上版本后，在项目目录执行 `sh start.sh` 即可直接启动，不依赖文件的可执行权限。若希望以后在 macOS Finder 中双击启动，首次执行 `chmod +x start.command start.sh`，之后右键 `start.command` 选择“打开”。脚本会创建隔离环境、安装依赖、启动 Gunicorn，并在服务就绪后打开 `http://127.0.0.1:5000`。换电脑或 Wi-Fi 后无需修改代码，局域网访问地址会在启动窗口重新计算；生产环境应在网关、DNS 和 HTTPS 下发布固定地址。

## 核心证据

- `GET /api/lineage/source/<record_id>`：从问题记录回溯来源系统、源表、业务主键、连接器状态和记录 SHA-256 指纹。
- `GET /api/graph`：展示来源系统、问题记录、黄金主数据、八步工作流、分发、反馈和哈希审计块的统一关系图。
- `GET /api/reports/governance`：输出可下发给源业务系统的质量整改报告及报告指纹。
- `POST /api/intent` + `POST /api/distribute`：自然语言解析品牌/型号/名称、目标系统和工厂，先确认计划再执行精确分发。
- `POST /api/explain`：基于真实来源记录、关键属性、冲突与命中规则生成治理解释；无大模型时使用事实模板。
- `POST /api/ocr`：PaddleOCR 本地识别、Qwen-VL 增强、规则兜底三级适配器。
- `GET/POST /api/ocr/install`：查询或启动受控的一键 OCR 安装任务，返回阶段进度和日志摘要。
- `GET /api/blockchain/verify`：验证 SHA-256 防篡改链式审计账本，并核对八步工作流当前状态是否与各步骤的最新指纹一致。该实现不是公链或联盟链共识网络。

## 环境配置

复制 `backend/.env.example` 为 `backend/.env`，按需填写：

- `DASHSCOPE_API_KEY`：通义 Embedding、Agent 解释和 Qwen-VL。
- `MDM_AUTH_PASSWORD`：启用浏览器 Basic Authentication。
- `MDM_DB_PATH`：SQLite 持久化路径。
- `MDM_HOST=0.0.0.0`：允许局域网访问。
- `MDM_PADDLEOCR_ENABLED=1`：启用本地 OCR 懒加载。
- `MDM_ALLOW_OCR_INSTALL=1`：允许服务器本机浏览器触发自动安装；设为 `0` 可关闭。
- `MDM_SAP_SOURCE_URL` / `MDM_ERP_SOURCE_URL` / `MDM_EAM_SOURCE_URL`：配置后，来源追溯可生成指向真实源业务对象的深链接。

首次 OCR 安装需要访问 Python/PyPI/Paddle 模型源，通常占用数分钟和较多磁盘空间。企业内网应把 Python 3.11、Python wheel 和 Paddle 模型放入内部制品库，再将安装脚本中的源地址替换为内网地址，而不是允许生产服务器直接访问公网。

不要把真实密钥、密码、`.env`、`.venv` 或运行数据库提交到代码仓库。

## Docker

基础版：

```bash
docker compose up -d --build
```

包含 PaddleOCR 的 CPU 镜像：

```powershell
$env:MDM_INSTALL_OCR="1"
docker compose up -d --build
```

数据和日志分别持久化到 `runtime/data` 与 `runtime/logs`。

## 测试

```powershell
python -m unittest -v test_api.py
python -m unittest -v test_chaos.py
```

健康检查：`GET /api/health`。响应中的 `ready=true` 只代表应用和数据库可用；OCR、大模型和远程 Embedding 的实际状态在 `capabilities` 中分别显示。

## 企业部署边界

当前 SQLite 版本适合竞赛、单节点试点和部门级验证。多服务器高可用、数百并发或跨地域生产部署应迁移到 PostgreSQL，并接入企业 SSO、HTTPS 网关、集中日志、密钥管理、定时备份与真实 SAP/EAM/MES/WMS 连接器。前端工厂视角用于演示，正式权限边界必须由后端身份与数据范围策略强制执行。
