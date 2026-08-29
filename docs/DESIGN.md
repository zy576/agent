# ForgeLoop 设计说明

## 1. 目标与合规边界

ForgeLoop 是一个小型、可解释的编程智能体。它不调用任何现成 Agent 产品，也不使用 Agent SDK 或服务端代码执行；唯一的远程能力是 DeepSeek Chat Completions 的原生 `tools/tool_calls`。对话历史、上下文压缩、JSON 参数解析、工具执行、错误恢复和终止状态机全部位于本仓库。

运行时只有 Python 标准库依赖。DeepSeek 接口按官方文档使用：

- [首次 API 调用](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

## 2. 架构

```text
CLI / 本机 Web 工作台
 ├─ Settings（只从环境变量读 key）
 ├─ WebApplication（会话状态、脱敏事件流、并发串行化）
 └─ CodingAgent（显式循环/终止门槛）
     ├─ DeepSeekClient（HTTP、重试、响应解析）
     ├─ ContextManager（完整工具组压缩）
     └─ ToolRegistry（Schema、参数解析、异常边界）
         └─ Workspace（路径策略、原子文件操作、子进程）
```

一次迭代的协议是：

1. 控制器把 system policy、原始任务和历史发给模型。
2. 模型返回普通 assistant 消息或一个/多个 `tool_calls`。
3. 控制器先保存完整 assistant 消息，再按 `tool_call_id` 顺序执行本地工具。
4. 每个结果编码为结构化 JSON，并以 `role=tool` 追加。
5. 没有工具调用的 assistant 报告是软终止；默认不设决策步数上限，重复调用、总工具调用量和总运行时间负责硬熔断；显式配置正数 `--max-steps` 时才启用有限步数终止。

V4 默认可能开启 thinking。项目显式发送 `thinking: {type: disabled}`，避免后续工具轮次必须回传推理字段的隐式协议负担；若服务仍返回 `reasoning_content`，客户端会保留它。

## 3. 工具集

| 工具 | 用途 | 关键约束 |
|---|---|---|
| `list_files` | 查看工作区结构 | 跳过依赖、缓存和 VCS 内部目录 |
| `read_file` | 分段读取 UTF-8 文本 | 行号、输出长度上限 |
| `search_files` | 文本/正则检索 | glob、匹配数与输出上限 |
| `write_file` | 新建或整文件写入 | 临时文件加 `os.replace` 原子落盘 |
| `replace_in_file` | 精确局部替换 | 出现次数不符即失败 |
| `run_command` | 构建、测试、运行程序 | `argv`、cwd 限制、超时、密钥环境剥离；Windows 的 `.cmd/.bat` 启动器经受限适配 |

工具异常不会击穿主循环，而会以 `{ok:false, tool, error}` 返回模型。命令非零退出属于可观察执行结果，不等同于工具自身崩溃。

## 4. 上下文管理

源头先限流：文件、搜索和命令输出在进入历史前保留头尾并截断。达到字符预算后，`ContextManager` 在单次模式保留 system policy 和原始任务；在交互模式保留 system policy、当前活动任务及待处理纠正，把更旧的闭合交互转换为确定性摘要，并优先保留当前轮工具证据。

assistant 的 `tool_calls` 与其随后全部 tool 结果被视为原子组，压缩时不会拆开，以免生成无配对 `tool_call_id` 的非法请求。完整原始历史仍由状态机持有，压缩只生成下一次 API 请求的副本。

`--interactive` 外层循环只在一轮正常返回后提交新的完整历史；每轮预算、重复计数和计时重新开始。若一轮改动后因预算停止，`verification_pending` 会传入下一轮，防止模型在未重新测试时直接宣告完成。

`--web` 复用相同的 `CodingAgent.run(history=..., verification_pending=...)` 接口。浏览器只提交当前任务，完整消息历史由 Python 后端独占；每轮使用非 daemon 工作线程运行，事件回调仅向有界队列写入脱敏记录，独立 HTTP 连接再以 NDJSON 实时读取，因此慢浏览器或刷新页面不会阻塞、取消智能体。服务同一时间只接受一个任务，完成后才原子提交新历史；若异常发生在可能访问工作区之后，会话进入 failed-closed，要求重启后先检查现场。

字符预算是保守近似，不声称等同 DeepSeek tokenizer；它的目的在于提供可预测的上界和退化行为。

## 5. 完成门槛与停止条件

- 正常完成：模型返回无 `tool_calls` 的最终报告。
- 验证门槛：最后一次写入后必须至少运行一次命令，且最新命令为零退出；否则控制器最多两次要求继续验证。
- 工具错误：结构化返回，模型可修正参数或换方案。
- API 错误：429/5xx 和连接错误指数退避并带 jitter；鉴权/请求错误立即失败。
- 重复循环：相同工具、参数和结果连续三次时警告，第四次终止。
- 预算：默认不限制模型决策轮次；单轮工具数、总工具数和总运行秒数始终设限。显式配置正数 `--max-steps` 时另加模型轮次上限；超限调用会补齐 skipped tool 结果再退出，历史仍保持协议闭合。总运行时间在模型与工具调用边界检查，正在执行的一次外部调用不会被抢占，因此实际结束时间可能超过配置值，但仍受对应的请求或命令超时约束。
- 用户中断：CLI 捕获 Ctrl+C 并以 130 退出。

## 6. 安全模型

文件路径经 `resolve()` 后必须位于 workspace；遍历到的每个文件也会重新解析，防止搜索沿符号链接越界；版本库内部、常见云凭据目录、`.env`、包管理器凭据和常见私钥文件被拒绝。写入采用同目录临时文件和原子替换，覆盖时保留普通权限位和原始换行/BOM；ACL、扩展属性等平台元数据不在保证范围。命令使用参数数组，cwd 同样受 workspace 校验，最长 300 秒；Windows 的 `.cmd/.bat` 启动器先解析绝对路径并拒绝 shell 元字符参数。子进程只继承 PATH、系统目录、临时目录、locale 以及常见非秘密构建路径等环境白名单；可重复使用 `--pass-env NAME` 显式加入非秘密变量，疑似 key/token/secret 的名称仍会被拒绝。stdout/stderr 先落临时文件再限量读取，超时会终止进程树。终端和可选 transcript 会遮盖当前 API key 及若干常见 token 形态，但 transcript 仍需提交前人工检查。

Web 服务固定绑定 `127.0.0.1`，端口默认由操作系统选择。每个请求校验 TCP peer 与精确 `Host`，变更请求额外要求精确同源 `Origin`、JSON Content-Type 和进程内高熵令牌；不提供 CORS 或任意静态路径。请求体、任务长度、事件日志和展示字段均有上限，并发送 CSP、no-store、nosniff、frame deny 等响应头。DeepSeek key、完整工具消息和原始模型历史不进入浏览器；所有可变内容通过 `textContent` 或 DOM 文本节点渲染。该边界防网页跨站调用与 DNS rebinding，不防同机恶意进程。

命令前后会对至多 20,000 个非缓存文件记录 size/mtime 轻量快照；若命令改了文件，该命令不能同时充当改后验证。它用于完成门槛和审计，不是强一致性证明，也不替代版本控制 diff。

这些措施是 policy guard，不是安全沙箱。被允许执行的 Python、编译器或测试程序仍可能访问当前账户可访问的资源或联网。面对不可信仓库，应在容器、虚拟机或低权限账户中运行。仓库内容也可能包含 prompt injection，因此 system policy 明确把文件与命令输出视作数据，但不能宣称纯提示词能提供绝对隔离。

## 7. 主要取舍

- 选择标准库 HTTP 而不是厂商 SDK：减少依赖并让请求、重试和解析逻辑可见；代价是功能较少。
- 选择参数数组：牺牲管道和重定向便利，换取更清楚的参数边界与更低注入风险。Windows 的 npm 等批处理启动器需要一个受限的 `cmd.exe` 兼容分支，这是明确记录的例外。
- 选择精确替换而不是自写完整 diff parser：小而可靠，匹配歧义会显式失败。
- 选择确定性摘要而不是额外模型调用：不增加费用且可测试；摘要语义质量有限，因此再次编辑前仍要求重读文件。
- 顺序执行多个工具：避免并发写冲突，代价是批量只读操作速度较慢。

## 8. 验证策略

`tests/` 使用脚本化假模型验证完整的“写入 - 测试 - 完成”循环，不消耗 API；同时覆盖坏 JSON、未知工具、路径逃逸、敏感文件、原子替换约束、非 shell 参数、超时、密钥环境剥离、上下文原子组、提前结束纠正、默认持续运行超过 24 次决策、重复循环和可选步数上限。Web 集成测试覆盖跨轮历史、并发互斥、事件积压回放、密钥脱敏、Host/Origin/token 校验、请求限流、CSP、路径穿越、安全 DOM 渲染、长任务轨迹折叠和刷新后的累计耗时恢复。真实 DeepSeek 演示再证明网络协议与模型决策可以端到端工作。
