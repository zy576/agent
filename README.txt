ForgeLoop - 从零实现的 CLI 编程智能体

仓库地址：待 GitHub 登录后创建公开仓库并替换本行。

环境：Python 3.10+，无第三方运行依赖。项目未使用 LangChain、LlamaIndex、OpenAI Agents SDK、AutoGen、CrewAI 或任何现成 Agent；只通过 DeepSeek 原生 tool calling 调用普通 Chat Completions API。

运行：
1. 设置环境变量 DEEPSEEK_API_KEY（不要写入文件）。
2. 在仓库根目录执行：python -m pip install -e .
3. 执行：python -m forgeloop --workspace 目标目录 "你的编程任务"
交互模式：python -m forgeloop --interactive --workspace 目标目录；每轮完成后可继续追问，输入 /quit 退出。
可用 DEEPSEEK_MODEL、DEEPSEEK_BASE_URL 或命令行参数覆盖模型与地址。默认模型为 deepseek-v4-pro。

测试：python -m unittest discover -s tests -v

特色：自主维护多轮历史与交互追问；模型可列出、搜索、分页读取、原子写入及精确替换文件，并以参数数组运行本地命令；工具错误会结构化回传供模型自修复；写入后必须再次验证，验证债务可跨交互轮保留；具备上下文压缩、API 重试、命令超时、重复循环与最大步数终止。文件路径被限制在 workspace 内，.git 与常见凭据文件不可读写，子进程仅继承最小环境变量。可用 --transcript 新建基础脱敏轨迹（不会覆盖已有文件，仍应人工检查）。

安全边界：命令策略只是减灾，不是操作系统沙箱；运行不可信任务时应使用容器或低权限账户。设计与答辩说明见 docs/。
