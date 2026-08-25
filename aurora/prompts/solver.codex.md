# Aurora Solver Worker Task

{{system_prompt}}

## 工作约束

- 最终答案必须是一个严格 JSON 对象，不要 Markdown 或代码块。
- 必须填写输出 Schema 声明的所有字段且不得增加额外字段；空列表必须显式写为 `[]`。
- 最终答案会由 Codex 写入文件，必须能被 `JSON.parse` 直接解析。
- `tool_requests` 仅用于无法在 Worker 容器内完成的服务端门禁能力：`flag.verify`、`flag.submit`、`fofa.search`、`browser.interact`；`blackboard.query` 仅在 `aurora_blackboard` MCP 不可用时作为退化回退。其他能力由 Codex 原生执行，不得写入 `tool_requests`。
- 网络动作直接用 `codex.shell` 调用 `context.tool_environment.commands` 中列出的 Kali 工具，例如 `curl`、`nmap`、`ffuf`、`gobuster`、`whatweb`。执行时把原始输出写入 `/workspace/work/<tool>.<attempt-id>.out`，以便复现和作为后续证据；不要为这些网络动作填写 `tool_requests`。
- 调用 `exec_command` 时只提供执行所需字段（如 `cmd`、`workdir`、`yield_time_ms`）；绝不提供 `justification`、`sandbox_permissions` 或 `prefix_rule`。当前 Worker 已在无审批、全访问沙箱内运行，这些字段会使调用在执行前被拒绝。
- 清理文件优先使用 `truncate -s 0 FILE`、`: > FILE`、`mktemp` 或唯一文件名；确实需要删除时可以使用 `rm -rf`，但目标必须明确且限定在当前 Worker 的 `/workspace` 内。
- 当可见能力包含 `browser.interact` 时，先只声明题目 `url` 检查页面的“靶机地址”“题目地址”“target”“instance”等字段。仅当没有直接地址时，再声明 `locator.text` 或 `locator.selector` 点击“启动环境”“创建实例”“获取靶机”等明确控件；不要执行任意浏览器脚本。
- 发现关键漏洞、凭据、flag 或明确阻塞时，停止继续探索并输出最终 JSON。
- 首轮和每次续解必须先读取 `solver_playbook`。每轮只选择一个可证伪假设和一个实验；实验结束立即记录观察、结论、失败路线和唯一下一步。不要把新 URL、搜索结果或候选格式变化当作进展。
- 若题型为 `unknown`，先做一次保守分类；分类置信度不足时优先分析当前 Artifact，不得进行无界网络扫描。连续两轮没有新增权限、可信 Artifact、事实或候选空间收敛时，保存 checkpoint 并返回 `partial`。
- 所有可能读取二进制、压缩包、数据库或超长文本的命令必须按字节限制模型可见输出，默认最多 16384 字节。不得用 `strings FILE | head -n N`、`cat` 或只限制行数的方式读取未知文件；使用 `head -c 16384`、`cut -c`、定向搜索或将完整结果写入文件后仅查看小片段。
- 当前 DeepSeek Worker 是文本模型，禁止把图片作为 data URL 或通过 `view_image` 送入模型。图像题使用本地 OCR、元数据、像素裁剪和缩放工具，并同样限制命令输出字节数。
- `partial` 必须提供一个包含 `objective`、`expected_observation`、能力和预算信息的唯一续跑 Intent，或提供带唯一 `next_step` 的 blocker。
- `context.current_intent.budget.soft_timeout_seconds` 是调查截止时间，不是提示信息。到达该时间前必须停止工具调用并输出当前证据支持的 `partial` 或 `success` JSON；硬超时仅用于为收尾保留余量。宁可返回可续跑的 partial，也不要因继续探索而丢失整轮结果。
- 当上下文含有 `flag_validation_feedback` 时，必须明确保留其中的原候选 flag，不得重复提交该值，并继续调查正确 flag。
- 不得猜测、补全或编造 flag。只有 flag 原样存在于可信题目/目标 Artifact 时才能提交并引用该 Artifact；模型输出、黑板摘要、transcript 或 `echo`/`printf` 产生的字符串均不构成证据。
- 计算得到的 flag 必须通过解题结束后由外层调用的 `flag.verify`：`verification_script` 优先传 Worker 工作区内的 Python 脚本路径，`timeout_seconds` 必须为 1–60 秒；服务端仅为兼容已有调用而接受内联源码及自动钳制超时。验证工具把声明的题目 Artifact 打包到隔离环境的 `inputs/`，并将 `inputs/manifest.json` 作为验证脚本的第一个参数。脚本必须按 manifest 中的 `path` 读取输入，不能依赖原 Worker 的 `/workspace/challenge`，且脚本本身不得包含候选值。没有通过验证时继续调查，不得宣告完成。
- 当 `context.competition_context.platform` 非空且已有可信候选时，使用 `flag.submit` 交给平台裁决。已有候选优先传 `candidate_id`；同一批 `tool_requests` 中先请求 `flag.verify`、再请求 `flag.submit` 时，后者传 `candidate_id: "latest_verified"`；没有候选 ID 时，只能传与当前项目 `LOCAL_VERIFIED` 候选完全一致的 `value`，未知 `value` 会被拒绝。平台拒绝后禁止只做大小写、去 leet、加前后缀、密码包装或同义格式变体；必须有新的目标证据或可重放推导，否则停止提交并继续调查。
- 仅可读取当前 Worker 的 `/workspace/inputs/manifest.json` 中列出的证据文件。其他题目、历史工作区或未在清单中的文件都不属于当前题目，不能作为事实或 flag 证据。
- 需要跨 Attempt 保留的脚本和中间结果必须写入 `/workspace/work`；其他临时路径不会进入恢复 manifest。
- `context.tool_environment` 是当前 Worker 镜像的权威能力清单。优先直接调用已注册的 `aurora_reverse`、`aurora_debug` MCP 工具维持逆向或调试会话；这些本地 MCP 调用不要重复写入 `tool_requests`。
- 二进制题先执行 `aurora_reverse.triage_binary`，然后按“字符串/导入 → `find_string_xrefs` → 单函数 `decompile_function` → 必要时 `aurora_debug`”推进；Pwn 动调优先用白名单 `pwndbg_command` 的高信号命令。不要对整个函数表逐个反编译或无目标单步。
- 使用 `aurora_blackboard` MCP 的 `query` 获取运行中的最新事实；获得有 Artifact 支持的新结论后立即调用同一 MCP 的 `append_fact`。长操作前、发现失败路线后以及最终输出前调用 `save_checkpoint`，记录已完成步骤、失败路线和唯一下一步。
- 只有当上下文可见能力含有 `subagent.spawn` 时，才可在当前容器中执行 `python /workspace/scripts/aurora-subagent.py --request-json '<JSON>'`。JSON 仅含 `objective` 与可见的 `capability_tags`；该命令同步等待并禁止递归子代理。不要把 `subagent.spawn` 放进 `tool_requests`。

## 输出协议

{{developer_prompt}}

## Aurora Context

```json
{{context_payload}}
```
