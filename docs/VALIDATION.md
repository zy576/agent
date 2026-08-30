# 验证记录

## 离线套件

以下是 2026-08-31 在 Python 3.12、已执行 `python -m pip install -e .` 后的本机记录：

```text
python -X dev -W error -m unittest discover -s tests -v
Ran 186 tests
OK (skipped=2)
```

Windows 当前账户无创建符号链接权限，且 POSIX 权限位用例只在类 Unix 平台适用，因此本机跳过 2 项；GitHub Actions 会在 Linux 的 Python 3.10、3.11、3.12 中实际运行它们。其余覆盖包括模型协议解析、429/网络重试、完整 Agent 状态机、多 tool-call 配对、写后验证门槛及跨交互轮验证债务、命令副作用追踪、超时与 Ctrl+C 进程树清理、默认持续运行超过 24 次决策、默认不限步数/工具数/总时长且显式配置才启用对应上限、循环熔断、活动任务上下文压缩、多轮历史恢复、交互退出语义、prompt injection 摘要隔离、路径与凭据规则、环境白名单、Windows batch 启动器、输出限流、CLI 退出码、transcript 拒绝覆盖，以及 Web 会话续轮、并发互斥、任意本机工作区浏览、工作区切换与任务准入原子性、跨工作区历史和验证状态重置、事件回放、密钥脱敏、同源防护、请求限流、静态资源安全头、安全 DOM 渲染、长任务轨迹折叠和刷新后的累计耗时恢复。

实验性只读子 Agent 的离线验收重点是：`--subagents` 只接受 `0..4` 且默认关闭；一个主任务至多委派一批；每个子 Agent 独立创建 client、history 和 context；只读工具 schema 与执行分派都拒绝写入、命令及递归委派；批内任务确实并发，但结果按输入顺序稳定返回；子 Agent 默认不限步数/调用数/时长，单报告及全局输出长度预算生效；单个子任务失败不会丢失其他结果；全部失败或工作区漂移会顶层失败并触发主 Agent 重新读取门槛；主 Agent 始终独占写入、命令执行、写后验证和完成判定。网络调用的在途请求不承诺被运行预算严格强杀。

## 真实并行子 Agent smoke test

- 日期：2026-08-30
- 模型：`deepseek-v4-pro`
- 配置：`--subagents 2 --max-steps 5`，隔离临时工作区。
- 任务：要求先将 `calculator.py` 缺陷检查与 `tests/test_calculator.py` 覆盖检查拆成两个独立只读子任务，再由主 Agent 汇总。
- 结果：两个子 Agent 均返回带行号证据，主 Agent 正确合并为统一报告，进程退出码为 0。
- 只读核验：运行前后两个文件 SHA-256 均一致，工作区始终只有原来的 2 个文件。

## 真实 DeepSeek smoke test

- 日期：2026-08-29
- 模型：`deepseek-v4-pro`
- 接口：`POST https://api.deepseek.com/chat/completions`
- 任务：修复 `add_many` 对生成器调用 `len()` 的缺陷，补回归测试且保持 `sum_pair` 行为。
- 轨迹：`list_files` -> 同一轮请求两个 `read_file`，控制器顺序执行 -> 两次 `replace_in_file` -> `run_command`。
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
