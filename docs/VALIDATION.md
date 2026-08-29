# 验证记录

## 离线套件

```text
python -X dev -W error -m unittest discover -s tests -v
Ran 103 tests
OK (skipped=2)
```

Windows 当前账户无创建符号链接权限，且 POSIX 权限位用例只在类 Unix 平台适用，因此本机跳过 2 项；GitHub Actions 会在 Linux 的 Python 3.10、3.11、3.12 中实际运行它们。其余覆盖包括模型协议解析、429/网络重试、完整 Agent 状态机、多 tool-call 配对、写后验证门槛及跨交互轮验证债务、命令副作用追踪、超时与 Ctrl+C 进程树清理、循环/步数/工具数/总时长预算、活动任务上下文压缩、多轮历史恢复、交互退出语义、prompt injection 摘要隔离、路径与凭据规则、环境白名单、Windows batch 启动器、输出限流、CLI 退出码、transcript 拒绝覆盖，以及 Web 会话续轮、并发互斥、事件回放、密钥脱敏、同源防护、请求限流、静态资源安全头和安全 DOM 渲染。

## 真实 DeepSeek smoke test

- 日期：2026-08-29
- 模型：`deepseek-v4-pro`
- 接口：`POST https://api.deepseek.com/chat/completions`
- 任务：修复 `add_many` 对生成器调用 `len()` 的缺陷，补回归测试且保持 `sum_pair` 行为。
- 轨迹：`list_files` -> 并行 `read_file` 两个文件 -> 两次 `replace_in_file` -> `run_command`。
- 结果：第 6 个模型决策结束；新增普通和空生成器测试；`python -m unittest discover -s tests` 共 5 项全部通过。
- 凭据检查：API key 只从 `DEEPSEEK_API_KEY` 读取，终端输出和最终报告均未出现 key。

可复现步骤：复制 `examples/demo_seed` 到临时目录，然后运行：

```powershell
python -m forgeloop --workspace <临时目录> --task-file examples/DEMO_TASK.txt
```

真实 API 测试不放入默认 CI，以避免消耗费用及向 CI 注入长期凭据。

## 真实交互 smoke test

- 第一轮：在全新示例副本中修复生成器缺陷、补 2 项测试并执行测试，5 项通过。
- 第二轮：在同一 `--interactive` 会话要求“不改文件，只用一句话总结上一轮”；模型保留上一轮历史，未调用工具并正确总结。
- 本地 `/quit` 正常结束，会话退出码为 0。

## 真实 Web 工作台 smoke test

- 以 `python -m forgeloop --web` 启动随机 localhost 端口，目标为隔离演示工作区。
- 从网页提交严格只读任务：“读取工作区文件列表并用一句话概括项目；不要修改文件，也不要运行命令”。
- 页面实时显示两次 DeepSeek 模型决策和一次 `list_files` 工具调用；输入在运行中禁用，完成后恢复会话能力。
- 最终报告正确概括 Python 计算器、单元测试及前端文件；轨迹以“本轮任务已完成”闭环。
- 本轮无写文件、替换文件或命令调用，DeepSeek key 未出现在浏览器状态和事件流。
