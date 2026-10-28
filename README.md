# 📚 KnowledgeBase MCP (`kb-mcp`)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Standard](https://img.shields.io/badge/MCP-Standard-green.svg)](https://modelcontextprotocol.io/)

A portable, mount-tolerant Model Context Protocol (MCP) server for personal knowledge bases on external storage (macOS APFS / external SSDs).

专为外置固态硬盘/大容量移动存储设计的个人知识库 MCP 智能服务，具备**热插拔防崩溃、混合语义与全文检索（FTS5 + BM25 + FastEmbed）、AI 自动化整理与原子大纲自愈**能力。

---

## 🌟 核心特性

- **即用即连与拔盘容错 (Mount-Tolerant)**：
  - 硬盘未连接时，服务优雅返回离线占位状态，**绝不导致 Claude Desktop 或 Cursor 启动崩溃**。
- **结构化收敛架构**：
  - 外置硬盘根目录下仅保留单一 `Database/` 文件夹，整洁清爽，内含待整理收件箱 `_Inbox` 与分类归档。
- **混合检索能力**：
  - 内置基于 SQLite FTS5 的 Trigram 中文子串分词 + BM25 排名，并可选 FastEmbed 本地向量语义召回。
- **AI 智能整理工作流**：
  - 提供 `kb_scan_inbox`、`kb_move_file`、`kb_rename_file`、`kb_update_index_md` 等工具，支持 AI 全自动内容分析、规范化重命名归档并同步更新 `INDEX.md`。

---

## 🚀 方式一：直接使用 GitHub 仓库调用（零安装 / 推荐）

如果你使用 **Claude Desktop**、**Cursor** 或任何支持 MCP 的客户端，只需在配置文件中直接引用该 GitHub 仓库，即可通过 `uvx` 自动拉取运行，无需手动克隆代码！

### Claude Desktop 配置示例
配置文件路径：`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "kb-retrieval": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/admin0330/kb-mcp.git",
        "kb-mcp"
      ],
      "env": {
        "KB_VOLUME": "/Volumes/KnowledgeBase/Database"
      }
    },
    "kb-files": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/Volumes/KnowledgeBase/Database"
      ]
    }
  }
}
```

> **提示**：如果需要启用向量语义检索，只需在 `args` 中添加 `--with` 和 `fastembed`，并设置环境变量 `KB_ENABLE_SEMANTIC: "1"`。

---

## 🛠 方式二：本地克隆与自包含部署

如果你希望将工具链与运行环境完全本地化（或随外置硬盘携带）：

```bash
# 1. 克隆仓库
git clone https://github.com/admin0330/kb-mcp.git
cd kb-mcp

# 2. 一键配置环境 (自动通过 uv 安装 Python 3.11 独立虚拟环境)
./scripts/setup.sh

# 3. 初始化外置硬盘目录结构 (首次使用)
./scripts/init_drive.sh /Volumes/KnowledgeBase
```

---

## 📂 外置硬盘标准目录规范

```text
/Volumes/KnowledgeBase/
└── Database/                  <-- 根目录唯一总入口
    ├── _Inbox/                <-- 【缓冲区】平时将待整理的文档、电子书、论文直接丢这里
    ├── 01_Documents/          <-- 通用文档、合同、方案、报告
    ├── 02_Books/              <-- 电子书、技术手册 (PDF/EPUB)
    ├── 03_Notes/              <-- 个人笔记、Markdown 卡片
    ├── 04_Archive/            <-- 历史归档文件
    ├── mcp/                   <-- 随盘携带的 MCP 系统与说明
    ├── .mcp_meta/             <-- SQLite 检索库 (index.db) 与日志 (organize.log)
    └── INDEX.md               <-- 全库大纲索引（AI 整理后自动同步）
```

---

## 🧰 提供的 MCP 工具清单

| 工具名称 | 功能描述 |
| :--- | :--- |
| `kb_status` | 报告硬盘挂载状态、分类目录以及已索引文件统计。 |
| `kb_search` | 全文/关键词/语义检索，返回命中的文档段落、匹配得分与来源路径。 |
| `kb_reindex` | 增量扫描外置硬盘中的新文件并更新索引库（支持 `full=True` 全量重构）。 |
| `kb_scan_inbox` | 扫描 `_Inbox/` 目录下的待整理文件并生成内容预览供 AI 决策。 |
| `kb_move_file` | 将文件从一个目录移动到另一个目录，并支持原子化重命名与索引同步。 |
| `kb_rename_file` | 对指定文件按标准规则重命名并同步索引。 |
| `kb_create_directory`| 在知识库内创建新的分类子目录。 |
| `kb_read_document` | 提取指定文档的全文纯文本（支持 PDF、Word、PPT、EPUB、Markdown 等）。 |
| `kb_list_tree` | 获取指定子目录的树状结构（支持深度限制）。 |
| `kb_log` | 记录整理流水账到外置硬盘日志中。 |
| `kb_update_index_md` | 重新生成并原子化覆盖根目录下的 `INDEX.md` 索引大纲。 |
| `kb_health_check` | 检查待处理积压、死链、未收录进 `INDEX.md` 的孤立文件。 |

---

## 💬 常用日常提示词模板

### 1. 【一键整理收件箱】
> “请调用 `kb_scan_inbox` 检查外置硬盘 `_Inbox` 里的所有新文件：
> 1. 根据内容为每个文件重命名为 `YYYY-MM-DD_[主题]_[关键词].[ext]`；
> 2. 移动到对应分类目录（如 `01_Documents`、`02_Books` 等）；
> 3. 调用 `kb_update_index_md` 更新根目录索引并调用 `kb_reindex` 增量更新检索库。”

### 2. 【智能检索与问答】
> “请在知识库中检索关于‘[关键词/技术点]’的资料，总结核心结论，并注明引用自哪个文件。”

### 3. 【知识库体检】
> “请运行 `kb_health_check` 检查知识库健康状态，并汇报积压与未索引情况。”

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
