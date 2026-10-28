# 📚 KnowledgeBase MCP (`kb-mcp`)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP Standard](https://img.shields.io/badge/MCP-Standard-green.svg)](https://modelcontextprotocol.io/)

A portable, mount-tolerant Model Context Protocol (MCP) server for personal knowledge bases on external storage (macOS APFS / external SSDs).

专为外置固态硬盘/大容量移动存储设计的个人知识库 MCP 智能服务，具备**热插拔防崩溃、混合语义与全文检索（FTS5 + BM25 + FastEmbed）、AI 自动化整理与原子大纲自愈**能力。

---

## 🌟 核心特性

- **即用即连与拔盘容错 (Mount-Tolerant)**：
  - 硬盘未连接时，服务优雅返回离线状态，**绝不导致 Claude Desktop 或 Cursor 启动崩溃**。
  - **晚插、拔插、只读挂载都不用重启 Claude**：插上硬盘后下一次调用立即可用（见下方「状态与故障诊断」）。
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
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/admin0330/kb-mcp.git",
        "kb-files-gateway",
        "--",
        "npx", "-y", "@modelcontextprotocol/server-filesystem",
        "/Volumes/KnowledgeBase/Database"
      ],
      "env": {
        "KB_VOLUME": "/Volumes/KnowledgeBase/Database"
      }
    }
  }
}
```

> `kb-files-gateway` 包在官方文件服务外面：硬盘没插时 Claude Desktop 照样能启动，插上后无需重启即可读写。
> 直接用 `npx … server-filesystem` 也能跑，但启动时硬盘不在就会直接退出，之后要重启 Claude Desktop 才能恢复。

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

# 3. 初始化外置硬盘目录结构 (首次使用；参数是知识库根目录，即 Database 文件夹)
./scripts/init_drive.sh /Volumes/KnowledgeBase/Database
```

然后在 `claude_desktop_config.json` 里把两个服务指向 `scripts/` 下的启动脚本（参考 `config/claude_desktop_config.json`，把路径换成你的克隆位置）。启动脚本会根据自身位置找到仓库，不依赖固定的用户目录。

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
| `kb_status` | 报告硬盘状态（细分到未挂载/只读/无权限/无响应等）、索引统计与后台重建进度；永不报错。 |
| `kb_search` | 全文/关键词/语义检索，返回命中的文档段落、匹配得分与来源路径。 |
| `kb_reindex` | 增量扫描新文件并更新索引（`full=True` 全量重构）。后台运行，超过 `wait_seconds` 先返回进度，用 `kb_status` 跟进。 |
| `kb_scan_inbox` | 扫描 `_Inbox/` 目录下的待整理文件并生成内容预览供 AI 决策。 |
| `kb_move_file` | 将文件从一个目录移动到另一个目录，并支持原子化重命名与索引同步。 |
| `kb_rename_file` | 对指定文件按标准规则重命名并同步索引。 |
| `kb_create_directory`| 在知识库内创建新的分类子目录。 |
| `kb_read_document` | 提取指定文档的全文纯文本（支持 PDF、Word、PPT、EPUB、Markdown 等）。 |
| `kb_list_tree` | 获取指定子目录的树状结构（支持深度限制）。 |
| `kb_log` | 记录整理流水账到外置硬盘日志中。 |
| `kb_update_index_md` | 重新生成并原子化覆盖根目录下的 `INDEX.md` 索引大纲。 |
| `kb_health_check` | 检查待处理积压、死链、未收录进 `INDEX.md` 的孤立文件。 |

`kb-files` 另外提供官方文件服务的全部工具（`read_text_file`、`write_file`、`list_directory`、`move_file` 等），外加它自己的 `kb_status`。

---

## 🩺 状态与故障诊断

两个服务的 `kb_status` 都会返回 `state` 字段，不是 `ok` 时附带 `hint` 告诉你怎么处理：

| state | 含义 | 处理 |
| :--- | :--- | :--- |
| `ok` | 已挂载、可读写 | — |
| `not_mounted` | 硬盘没插或没挂载 | 插上即可，无需重启 Claude |
| `phantom_dir` | 路径是系统盘上的残留文件夹，不是外置硬盘 | 推出硬盘、删掉残留文件夹后重插；`candidates` 会列出硬盘实际挂载的位置（如 `KnowledgeBase 1`） |
| `kb_dir_missing` | 硬盘已挂载，但找不到 `Database` 文件夹 | 是否插错盘、改了名，或还没运行 `init_drive.sh` |
| `permission_denied` | 系统拒绝访问 | 系统设置 → 隐私与安全性 → 文件与文件夹 → 允许 Claude 访问“可移除的宗卷” |
| `read_only` | 只读挂载 | 检索、读取照常可用，写入会被拒绝；用“磁盘工具”急救后重插 |
| `disk_full` | 硬盘已满 | 清理空间 |
| `unresponsive` | 检查在超时内没完成（休眠、接触不良、正在推出） | 稍等重试，持续出现就重新插拔 |
| `io_error` | 其他读写错误 | 重新插拔，必要时“磁盘工具”急救 |

这一版专门针对「拿不到状态」做了加固：

- **晚插硬盘不用重启**：Claude Desktop 只在启动时读取一次工具列表，并且会忽略后续的列表变更通知。所以 `kb-files` 改由 `fs_gateway.py` 常驻代理：始终声明完整的文件工具，硬盘可用时才在第一次调用时拉起官方文件服务；文件服务崩溃了，下一次调用会自动重启它。
- **状态永不卡死**：所有耗时操作都放到工作线程里跑，重建索引、读大 PDF 时 `kb_status` 照样秒回；硬盘检查有超时（默认 4 秒），超时报 `unresponsive`，不会一直挂着。
- **拔盘不崩溃**：SQLite 的 WAL 模式会内存映射 `-shm` 文件，硬盘被拔掉时 macOS 会用 SIGBUS 直接杀掉进程。索引库因此改用回滚日志（`journal_mode=DELETE`，旧索引首次打开时自动转换）并关闭 mmap；每次操作单独开、关连接，拔插后不会残留失效的连接。
- **拔盘不丢索引**：重建分批提交；中途断开或有目录读不了时，不会把读不到的文件当成“已删除”清出索引。

可选环境变量：

| 变量 | 默认 | 作用 |
| :--- | :--- | :--- |
| `KB_VOLUME` | `/Volumes/KnowledgeBase/Database` | 知识库根目录 |
| `KB_PROBE_TIMEOUT` | `4` | 硬盘检查超时（秒） |
| `KB_REQUIRE_EXTERNAL` | 路径在 `/Volumes/` 下时为 `1` | 要求路径确实在外置卷上（防止写进系统盘残留文件夹） |
| `KB_FS_GATEWAY` | `1` | 设为 `0` 恢复旧的 kb-files 启动方式 |
| `KB_FS_START_TIMEOUT` | `45` | 官方文件服务启动超时（秒） |
| `KB_CACHE_DIR` | `~/.cache/kb-mcp` | kb-files 工具列表缓存位置 |
| `KB_SQLITE_JOURNAL` | `DELETE` | 索引库日志模式（不建议改回 `WAL`） |

测试：

```bash
python3 -m unittest tests/test_health.py tests/test_indexer.py   # 纯标准库，任何系统
server/.venv/bin/python tests/e2e_macos.py                       # macOS：用临时磁盘镜像模拟插拔
```

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
