# M-AI Master 4.2

面向大型制造集团的 AI 物料主数据智能治理与共享演示系统。系统以 Flask + SQLite 为权威运行端，完成多源数据接入、标准识别、Embedding 向量入库、图谱辅助查重、多主体审核、质量评估、跨工厂分发和工厂反馈回流八步闭环。

## Windows 运行

建议安装 Python 3.11 x64，并在安装时启用 `Add Python to PATH`。

1. 普通稳定版：双击 `start.bat`。
2. 真实本地 OCR：先双击 `install-ocr.bat`，安装完成后双击 `start-ocr.bat`。
3. 浏览器访问启动窗口显示的 `http://127.0.0.1:5000`；局域网设备使用窗口打印的 `LAN` 地址。

普通稳定版不安装大型 OCR 依赖。此时 `/api/ocr` 会尝试 Qwen-VL；若密钥或网络不可用，再明确降级为规则解析，治理流程不会崩溃。真实 OCR 版在独立 `.venv-ocr` 中运行，不影响普通 `.venv`。

## 核心证据

- `evaluation/ground_truth_50.csv`：50 条独立人工分组真值标签，共 27 个真值组。
- `POST /api/evaluation/run`：输出 Pairwise Precision/Recall/F1、B³ 聚类指标、阈值扫描、数据集哈希和错误样例。
- `POST /api/explain`：基于真实来源记录、关键属性、冲突与命中规则生成治理解释；无大模型时使用事实模板。
- `POST /api/ocr`：PaddleOCR 本地识别、Qwen-VL 增强、规则兜底三级适配器。
- `GET /api/blockchain/verify`：验证 SHA-256 防篡改链式审计账本。该实现不是公链或联盟链共识网络。

## 环境配置

复制 `backend/.env.example` 为 `backend/.env`，按需填写：

- `DASHSCOPE_API_KEY`：通义 Embedding、Agent 解释和 Qwen-VL。
- `MDM_AUTH_PASSWORD`：启用浏览器 Basic Authentication。
- `MDM_DB_PATH`：SQLite 持久化路径。
- `MDM_HOST=0.0.0.0`：允许局域网访问。
- `MDM_PADDLEOCR_ENABLED=1`：启用本地 OCR 懒加载。

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
