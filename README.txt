ForgeLoop - 从零实现的 DeepSeek 编程智能体

仓库：https://github.com/zy576/agent
环境：Python 3.10+，仅依赖标准库；未使用 Agent 框架。Agent 循环、历史、上下文、工具分派、错误恢复与终止条件均自行实现。

启动：
1. 设置环境变量 DEEPSEEK_API_KEY，切勿写入仓库。
2. 仓库根目录运行：python -m pip install -e .
3. Web：python -m forgeloop --web --workspace 目标目录
4. 单任务：python -m forgeloop --workspace 目标目录 "任务"
5. 连续对话：python -m forgeloop --interactive --workspace 目标目录

只读子 Agent：追加 --subagents N（0 至 4，默认关闭）。一个主任务至多委派一批；每个子 Agent 有独立 client、history、context，只能列出、读取、搜索文件，并受 6 个工具决策步骤、24 次调用、120 秒及报告长度限制。批内并发、结果按输入顺序返回；主 Agent 独占写入、命令和最终验证。在途 HTTP 请求不能被线程强杀，此机制也不是操作系统沙箱。

能力：原生 DeepSeek tool calling；文件读写、搜索、精确替换；参数数组执行命令；结构化错误恢复；写后验证；多轮历史；上下文压缩；API 重试；重复循环、工具数与运行时间熔断。默认无固定决策步数，可用 --max-steps N 限制。

安全：文件工具限制在 workspace 并拒绝 .git、.env、私钥等路径；子进程仅继承最小环境。Web 仅监听 127.0.0.1；所有请求校验来源与 Host，接口使用随机令牌，任务提交另验 Origin。不可信项目应在容器、虚拟机或低权限账户中运行。

测试（安装后）：python -X dev -W error -m unittest discover -s tests -v
设计、验证与答辩材料见 docs/。
