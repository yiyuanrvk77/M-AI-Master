# M-AI Master 5.1

面向大型制造集团的 AI 物料主数据智能治理与共享系统。系统以 Flask + SQLite 为权威运行端，完成多源数据接入、标准识别、Embedding 向量入库、图谱辅助查重、多主体审核、质量评估、跨工厂分发和工厂反馈回流八步闭环。5.1 将文本、语音、图片、检索、RAG、分发与治理编排统一到主数据 Agent 工作台，并保留服务端 RBAC、工厂数据域、CSRF、HMAC 签名审计、数据分级与保留策略，以及可导入、可发布、可回滚的版本化 RAG 标准知识库。

## Windows 运行

建议安装 Python 3.11 x64，并在安装时启用 `Add Python to PATH`。

1. 普通稳定版：双击 `start.bat`。
2. 浏览器访问启动窗口显示的 `http://127.0.0.1:5000`；首次启动会在窗口中显示初始凭据文件位置。
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
- `POST /api/auth/login` + `GET /api/auth/me`：服务端会话、HttpOnly Cookie、CSRF 与身份绑定的工厂数据域。
- `GET /api/security/audit/verify`：验证独立 HMAC-SHA256 安全事件链，可检测数据库内的审计记录篡改。
- `GET /api/compliance/status`：输出数据分级、外部 AI 出域策略、保留期和法律保全控制证据。
- `GET /api/governance/catalog`：输出 DMBOK2 工程控制、RACI 责任、业务/技术/安全元数据和数据质量规则目录。
- `GET/PATCH /api/governance/issues`：按登录身份与工厂范围查询、认领和关闭数据质量问题，处置证据写入签名安全审计。
- `PATCH /api/governance/controls/<control_code>`：由授权角色评估控制成熟度、状态与证据，不以界面百分比替代正式认证。
- `GET /api/standards/stats` + `/api/knowledge/*`：管理 72 条分类、12 条别名规则和 10 条标准依据，支持草稿、校验、发布、回滚、重建索引和行级引用。

## DMBOK2 治理整改

系统将 DMBOK2 的数据治理、数据架构、数据存储与操作、数据安全、数据集成与互操作、参考数据与主数据、元数据、数据质量及文档生命周期要求映射为 14 项可验证工程控制。当前 12 项达到 `IMPLEMENTED` 或 `MONITORED`；真实业务连接器和备份恢复演练保留为 `DESIGNED`，不使用模拟结果替代企业验收。

数据接入和治理会按当前批次动态执行 9 条质量规则，并生成带来源系统、来源业务主键、责任角色、严重度、截止时间和处置证据的质量问题。问题状态为 `OPEN -> ACKNOWLEDGED -> RESOLVED/WAIVED`。质量报告中的“准确性代理”只描述字段和结构规则，不等于 Precision/Recall；业务准确率仍需业务方签字的独立验证集。

治理与合规控制通过受保护接口、签名审计和技术文档提供证据，不在业务前端增加独立页面。工程控制覆盖率不是 DAMA 成熟度认证分数；等保、法律合规和企业制度仍需部署单位正式评估。

## 企业账号与权限

首次启动自动建立四个账号，密码只在 `runtime/data/initial-credentials.txt` 中生成一次，数据库只保存 scrypt 哈希。

| 账号 | 角色 | 核心权限 |
|---|---|---|
| `group_admin` | 集团系统管理员 | 用户、知识版本、策略和数据清理 |
| `group_approver` | 集团审批与分发负责人 | 生命周期终审、已批准主数据分发 |
| `shanghai_steward` | 上海工厂主数据管理员 | 仅上海工厂接入、审核、反馈 |
| `compliance_auditor` | 安全合规审计员 | 只读报告、血缘、合规状态和审计验签 |

生产部署应通过环境变量提供初始密码或将账号对接企业 SSO，并在 HTTPS 网关后设置 `MDM_COOKIE_SECURE=1`。

## RAG 知识库配置

默认启动时导入 `SY_T5497-2018备品备件分类树(1).xlsx`，并原子发布为活动版本。检索同时融合结构化规则、词法重合和本地特征哈希向量，所以无 API Key 或断网时仍可检索。主要配置：

- `MDM_RAG_SOURCE_PATH`、`MDM_RAG_VERSION`：启动数据源与标准版本。
- `MDM_RAG_RULE_WEIGHT`、`MDM_RAG_LEXICAL_WEIGHT`、`MDM_RAG_VECTOR_WEIGHT`：三路检索权重。
- `MDM_RAG_AUTO_ACCEPT_THRESHOLD`、`MDM_RAG_MINIMUM_MARGIN`：自动推荐阈值与 Top1/Top2 最小差距。
- `MDM_RAG_ALLOWED_PLANTS`、`MDM_RAG_ALLOWED_CLASSIFICATIONS`：知识版本的工厂与密级访问范围。

管理员上传新 XLSX 后先得到 `DRAFT`，校验通过后才能 `PUBLISHED`；切换和回滚在单个 SQLite 写事务中完成，检索不会读到半成品索引。

## 环境配置

复制 `backend/.env.example` 为 `backend/.env`，按需填写：

- `DASHSCOPE_API_KEY`：通义 Embedding、Agent 解释和 Qwen-VL。
- `MDM_SECURITY_MODE=enterprise`：启用企业会话、RBAC、CSRF 和工厂数据域。
- `MDM_PASSWORD_GROUP_ADMIN` 等四个变量：可选的固定初始密码；留空则安全生成。
- `MDM_RAG_*`：配置知识源、版本、权重、阈值和 ACL。
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

数据、会话签名密钥和初始凭据持久化到 `runtime/data`，日志持久化到 `runtime/logs`。

## 测试

```powershell
python -m unittest -v test_api.py
python -m unittest -v test_chaos.py
python -m unittest -v test_enterprise.py
```

当前回归基线为 40/40：`test_api.py` 21 项、`test_chaos.py` 9 项、`test_enterprise.py` 10 项。企业测试覆盖统一 Agent、单任务分发、四账号权限、CSRF、跨工厂越权阻断、法律保全、HMAC 篡改检测、RAG 兼容性、DMBOK 控制目录和质量问题闭环。

健康检查：`GET /api/health`。响应中的 `ready=true` 只代表应用和数据库可用；OCR、大模型和远程 Embedding 的实际状态在 `capabilities` 中分别显示。

## 技术文档生成

文档源文件为 `M-AI Master技术说明文档V4.3.docx`，生成脚本会按当前代码、DMBOK2 控制目录和企业 Agent 能力生成 V5.1 文档，并同步导出 PDF：

```powershell
python -m pip install -r requirements-docs.txt
python update_dmbok_document.py
python export_technical_pdf.py
```

生成结果为 `M-AI Master技术说明文档V5.1_企业Agent增强版_最新版.docx` 和同名 PDF。脚本会清理已删除的旧接口和过期测试口径，避免把临时代码行号、人工真值评测或未实现能力写入交付文档。

## 企业部署边界

当前 SQLite 版本适合竞赛、单节点试点和部门级验证。多服务器高可用、数百并发或跨地域生产部署应迁移到 PostgreSQL，并接入企业 SSO、HTTPS 网关、集中日志、密钥管理、WORM/外部审计锚定、加密备份与真实 SAP/EAM/MES/WMS 连接器。当前功能形成了安全控制与可审计证据，不代表已通过等保、数据合规或法律认证。
