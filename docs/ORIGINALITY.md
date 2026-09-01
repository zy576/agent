# 原创性与合规自查（非套壳声明）

本文件回答一个核心问题：ForgeLoop 是不是「套壳」或「照抄」DeepSeek Harness？
结论：**不是**。ForgeLoop 是独立实现；DeepSeek Harness 仅作为交互与功能设计的参考对象（题目允许迁移功能），没有任何代码照搬。

## 一、硬证据

### 1. 零框架依赖
`pyproject.toml` 的 `dependencies` 为空列表，运行时只使用 Python 标准库。
未使用 LangChain / LlamaIndex / OpenAI Agents SDK / Claude Agent SDK / AutoGen / CrewAI 等任何 agent 框架或 SDK。

### 2. 仓库内零外部引用
全库源码中不包含 `deepseek-harness`、`@deepseek-ai/dsh-*`、`figma` 等任何对参考项目或第三方框架的引用。

### 3. 逐行相似度扫描
把 ForgeLoop 全部源码（Python/JS/CSS/HTML）中长度 ≥ 30 字符的 4488 行唯一代码行，在 DeepSeek Harness 的全部前端/后端源码（排除 node_modules、构建产物、测试）中做固定串匹配：
- 命中 83 行，**全部是通用 CSS 样板**（`justify-content: space-between`、`border: 1px solid transparent`、`@media (prefers-reduced-motion: reduce)`、`background: linear-gradient(` 等）以及两条通用 JS 惯用法（`for (const line of lines)`、`body: JSON.stringify(`）；
- **零条注释、零条逻辑代码、零条 docstring、零个布局样式块**与参考项目重合；
- 少数短界面文案（如「新会话」「重命名」「归档会话」「删除工作区」等）与参考项目相同——它们是不可替代的功能性短标签，属于交互设计参照而非代码搬运。

## 二、题目要求的核心逻辑全部自研（逐条对应）

题目明确「重要逻辑需自行编写」：对话历史与上下文管理、工具的定义与本地执行、模型输出的解析、循环终止条件、错误处理。

| 要求 | 自研实现位置 |
|---|---|
| 对话历史与上下文管理 | `context.py`（确定性按工具调用组压缩、预算、不变量保护）、`agent.py` 的 `_resume_messages` / 历史校验 |
| 工具的定义与本地执行 | `tools.py`（JSON Schema 定义、路径边界、凭据防护、原子写入、无 shell 子进程、超时与进程树清理、工作区切换与目录浏览） |
| 模型输出的解析 | `client.py`（DeepSeek Chat Completions 协议解析、tool_calls 校验与配对）、`agent.py` 的 `_tool_name/_tool_arguments` |
| 循环终止条件 | `agent.py`（无工具调用的最终报告、写后验证门槛、重复熔断、可选步数/工具数/时长预算（默认不限）、finalization 收尾） |
| 错误处理 | `client.py` 重试策略、`tools.py` 结构化错误、`web.py` 会话失败关闭（failed-closed）、子 Agent 协调错误语义 |

其余模块（Web 会话状态机、持久化存储、前端全部 JS/CSS）均为本仓库独立编写，代码风格、数据结构、命名体系与参考项目完全不同。

## 三、从 DeepSeek Harness 迁移的是「功能与交互设计」，不是代码

迁移清单（每一处都是按参考项目的用户行为描述，用自研代码重新实现）：
- 「新会话先选工作区」的流程（新会话 → 系统原生「浏览文件夹」对话框 → 选定后新建会话）；
- 工作区切换入口（顶栏「目标工作区」与侧栏均为系统原生文件夹选择器，覆盖全盘任意文件夹）；
- 左侧工作区/会话树形栏（展开收起、状态点、相对时间、悬停卡片、行内操作菜单）；
- 会话管理能力（重命名、分叉、归档/恢复、删除）与多工作区持久化。

未迁移（判断为不适合或价值低）：拖拽排序、视图分组/排序切换、应用内目录浏览器（由系统原生选择器替代）、实时内容搜索。

## 四、为什么不是「套壳」

「套壳」指把现成 agent 产品（Claude Code、Codex、DeepSeek Harness 等）包一层界面。ForgeLoop 不封装任何现成 agent 产品，也不调用任何托管代码执行服务：它通过自己编写的 HTTP 客户端直接调用 DeepSeek Chat Completions 的原生 tool calling 接口（题目明确允许「模型厂商的 API 客户端库、OpenAI 兼容网关及模型原生的 tool calling 接口」），Agent 循环、上下文管理、工具执行、终止条件与错误处理全部在本仓库实现。
