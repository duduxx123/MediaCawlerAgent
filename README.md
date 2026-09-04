# MediaCrawlerAgent

一个面向学习和研究的多平台内容采集与智能体项目。项目在 MediaCrawler 的基础上整合了 LangChain/LangGraph 智能体、WebUI、SQLite 数据镜像、评论分析和部分平台互动能力。

> 本项目仅供学习、研究和本地授权场景使用。使用时请遵守目标平台的服务条款、隐私规定和适用法律，不要进行大规模、高频或未经授权的采集和互动。详见 [LICENSE](LICENSE)。

## 功能概览

### 多平台采集

支持以下平台的关键词搜索、指定内容详情和创作者内容采集（具体能力受平台页面和登录状态影响）：

| 平台 | 标识 | 内容采集 | 评论采集 | 词云 |
| --- | --- | --- | --- | --- |
| 小红书 | `xhs` | ✅ | ✅ | ✅ |
| 抖音 | `dy` | ✅ | ✅ | ✅ |
| 快手 | `ks` | ✅ | ✅ | ✅ |
| B站 | `bili` | ✅ | ✅ | ✅ |
| 微博 | `wb` | ✅ | ✅ | ✅ |
| 百度贴吧 | `tieba` | ✅ | ✅ | ✅ |
| 知乎 | `zhihu` | ✅ | ✅ | ✅ |

### 智能体工具

智能体可以通过自然语言调用以下工具：

- 采集：`crawl_by_keywords`、`crawl_specified_ids`、`crawl_creator`
- 数据：`read_crawled_data`、`list_crawled_files`
- 抖音：读取评论用户、发表评论、回复评论、发送私信
- B站：发布视频/动态评论，以及楼中楼回复
- 小红书：准备私信草稿并在确认后发送
- 快手：在作品下发布一级评论

其中评论、回复和私信属于真实写操作，必须由用户明确提出，并使用已登录的平台账号。小红书私信采用“先填草稿、再确认发送”的两步流程；当前快手工具只支持作品一级评论，不支持指定评论回复。

### WebUI 与数据能力

- WebUI 可视化配置爬取任务、查看实时日志和预览数据
- 内置智能体聊天界面，支持流式回复和工具调用状态
- 评论词云生成，可通过 `ENABLE_GET_WORDCLOUD` 开关控制
- JSON/JSONL 等文件存储，同时可镜像写入本地 SQLite
- 支持创作者线索数据的本地查看和管理
- Agent 会话默认只保存在内存；可选用 SQLite 持久化会话

## 项目结构

```text
MediaCrawler/
├─ agent/              # LangGraph 智能体、提示词和工具封装
├─ api/                # FastAPI API、WebSocket 和 WebUI 服务
├─ config/             # 通用及各平台配置
├─ database/           # SQLAlchemy 模型和数据库初始化
├─ media_platform/     # 各平台客户端、登录和采集实现
├─ store/              # CSV/JSON/JSONL/SQLite 等存储实现
├─ tools/              # 浏览器、迁移、词云等基础工具
├─ html/               # 旧版静态页面及相关资源
├─ webui/              # React + Vite 可视化前端
├─ tests/              # Agent、API 和平台工具测试
├─ main.py             # 爬虫命令行入口
└─ .env.example        # 环境变量模板，不包含真实密钥
```

## 环境要求

- Windows、macOS 或 Linux
- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Node.js 16 或更高版本（仅构建 WebUI 时需要）
- Chrome 或 Chromium 浏览器

项目默认使用 Chrome DevTools Protocol（CDP）连接已有浏览器，以复用用户主动登录的账号状态。使用 CDP 时通常不需要额外安装 Playwright 浏览器；如果改用独立 Playwright 浏览器，再执行：

```shell
uv run playwright install
```

## 安装

```shell
git clone https://github.com/duduxx123/MediaCawlerAgent.git
cd MediaCawlerAgent
uv sync
```

复制环境变量模板并填写智能体所需的模型密钥：

```shell
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

至少配置一个：

```dotenv
DEEPSEEK_API_KEY=你的密钥
```

也可以使用其他 OpenAI 兼容服务：

```dotenv
AGENT_LLM_API_KEY=你的密钥
AGENT_LLM_BASE_URL=https://api.example.com/v1
AGENT_LLM_MODEL=你的模型名
```

`.env` 已被 Git 忽略，禁止把真实密钥写入代码、配置示例或提交记录。

## 浏览器与登录

通用配置位于 [config/base_config.py](config/base_config.py)：

```python
ENABLE_CDP_MODE = True
CDP_DEBUG_PORT = 9222
CDP_CONNECT_EXISTING = True
SAVE_LOGIN_STATE = True
```

启动一个允许远程调试的 Chrome 实例，然后在浏览器中登录目标平台。也可以在 Chrome 地址栏打开：

```text
chrome://inspect/#remote-debugging
```

启用远程调试后，确认调试端口与 `CDP_DEBUG_PORT` 一致。首次访问某个平台时，若浏览器出现授权提示，请在确认是本项目发起的情况下允许连接。

如不想连接已有浏览器，可将 `CDP_CONNECT_EXISTING` 改为 `False`，项目会使用独立的浏览器用户目录。登录数据保存在本地 `browser_data/`，该目录不会被 Git 托管。

## 命令行采集

在 [config/base_config.py](config/base_config.py) 和各平台配置文件中设置关键词、ID 列表、评论数量等参数，然后运行：

```shell
# 关键词搜索
uv run main.py --platform xhs --lt qrcode --type search

# 抓取指定内容详情
uv run main.py --platform dy --lt qrcode --type detail

# 查看全部命令参数
uv run main.py --help
```

平台标识为：`xhs`、`dy`、`ks`、`bili`、`wb`、`tieba`、`zhihu`。

常用配置：

```python
ENABLE_GET_COMMENTS = True
ENABLE_GET_WORDCLOUD = True
CRAWLER_MAX_NOTES_COUNT = 15
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 10
SAVE_DATA_OPTION = "jsonl"
ENABLE_SQLITE_MIRROR = True
```

## 启动智能体

### 命令行对话

```shell
uv run python -m agent.main
```

也可以执行一次性问答：

```shell
uv run python -m agent.main "搜索抖音伽摩cos并读取评论"
```

命令行内置命令：

```text
/tools   查看工具清单
/clear   清空当前会话
/exit    退出
```

### 会话记忆

默认使用内存会话，程序退出后聊天记录会清除。如果需要跨进程或重启保留 Agent 会话，可在 `.env` 中设置：

```dotenv
AGENT_MEMORY_DB=memory/agent.sqlite3
```

`memory/` 是本地运行目录，包含聊天内容，不应提交到公共仓库。

## 启动 WebUI

开发模式需要两个终端：

```shell
# 终端 1：启动 API 和智能体后端
uv run uvicorn api.main:app --port 8080 --reload

# 终端 2：启动前端开发服务器
cd webui
npm install
npm run dev
```

然后访问 <http://localhost:5173>。

构建生产前端：

```shell
cd webui
npm install
npm run build

cd ..
uv run uvicorn api.main:app --port 8080
```

构建完成后访问 <http://localhost:8080>。健康检查地址为 <http://localhost:8080/api/health>。

## 数据保存

项目支持 CSV、JSON、JSONL、Excel、SQLite、MySQL、MongoDB 和 PostgreSQL 等存储方式。默认配置为 JSONL，并将数据镜像写入 SQLite，便于 WebUI 和 Agent 查询。

SQLite 相关文件默认位于：

```text
database/sqlite_tables.db
```

初始化并直接使用 SQLite：

```shell
uv run main.py --init_db sqlite
uv run main.py --platform xhs --lt qrcode --type search --save_data_option sqlite
```

详细说明见 [docs/data_storage_guide.md](docs/data_storage_guide.md)。历史 JSONL 数据迁移可参考 [tools/migrate_jsonl_to_sqlite.py](tools/migrate_jsonl_to_sqlite.py)。

## 隐私与安全

以下内容只应保存在本机，已加入 `.gitignore`：

- `.env`、模型 API Key 和代理凭据
- `browser_data/`、Cookie、登录态和浏览器用户目录
- `data/`、`database/*.db`、`memory/` 和 `logs/`
- 测试缓存、临时调试脚本、截图和构建产物

项目配置中的 `DY_SAVE_ORIGINAL_USER_INFO`、`XHS_SAVE_ORIGINAL_USER_INFO` 等选项可能保存平台返回的用户标识。开启前请确认具备必要授权，并限制在合法、必要的使用范围内。

## 测试

运行完整测试：

```shell
uv run pytest -q
```

部分代理池和 Redis 测试需要本机 Redis 服务运行在 `127.0.0.1:6379`；没有 Redis 时，这些外部依赖测试会失败，但不影响不依赖 Redis 的单元测试。

## 许可证与免责声明

本项目遵循仓库中的 [LICENSE](LICENSE)。项目内容仅供学习和研究，使用者应自行确认数据来源、采集频率、账号授权、个人信息处理和互动行为符合目标平台规则及适用法律。开发者不对未经授权使用本项目造成的后果负责。
