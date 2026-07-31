# M-AI Master

AI 主数据智能治理与智能共享演示系统，面向大型制造集团的多系统物料主数据治理场景。

## 能力

- CSV 主数据接入与字段映射
- 通义 text-embedding-v3 语义向量优先，网络或 API Key 不可用时自动降级为 Jaccard 字符相似度
- 规则增强的相似度查重、硬冲突拦截和人工审核队列
- SY/T 5497-2018 备品备件分类树与标准规则库
- 主数据申请、审批、变更、归档生命周期
- 自然语言解析目标系统、工厂主体和分发模式
- SAP、EAM、MES、WMS 模拟分发适配器与可追溯日志

## 快速运行

### Windows

双击 `start.bat`。脚本会检查 Python 和依赖，并启动 `http://127.0.0.1:5000`。

### Linux/macOS

```bash
./start.sh
```

也可以手动运行：

```bash
python -m pip install -r backend/requirements.txt
python backend/app.py
```

## 可选配置

复制 `backend/.env.example` 为 `backend/.env`，配置 `DASHSCOPE_API_KEY` 后启用通义向量服务。未配置或网络不可用时，系统自动使用本地降级算法，演示仍可运行。

## 目录

- `backend/app.py`: Flask 后端与治理 API
- `backend/index.html`: 离线单页前端
- `frontend/`: 前端源码副本
- `备品备件脏数据.csv`: 演示数据
- `SY_T5497-2018备品备件分类树(1).xlsx`: 分类树数据
