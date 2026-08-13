# Aurora Solver Worker Task

{{system_prompt}}

## 工作约束

- 最终答案必须是一个严格 JSON 对象，不要 Markdown 或代码块。
- 最终答案会由 Codex 写入文件，必须能被 `JSON.parse` 直接解析。
- 如需外层受控能力网关执行操作，在 `tool_requests` 中声明；否则该数组可以为空。
- 当可见能力包含 `browser.interact` 时，先只声明题目 `url` 检查页面的“靶机地址”“题目地址”“target”“instance”等字段。仅当没有直接地址时，再声明 `locator.text` 或 `locator.selector` 点击“启动环境”“创建实例”“获取靶机”等明确控件；不要执行任意浏览器脚本。
- 发现关键漏洞、凭据、flag 或明确阻塞时，停止继续探索并输出最终 JSON。
- 所有可能读取二进制、压缩包、数据库或超长文本的命令必须按字节限制模型可见输出，默认最多 16384 字节。不得用 `strings FILE | head -n N`、`cat` 或只限制行数的方式读取未知文件；使用 `head -c 16384`、`cut -c`、定向搜索或将完整结果写入文件后仅查看小片段。
- 当前 DeepSeek Worker 是文本模型，禁止把图片作为 data URL 或通过 `view_image` 送入模型。图像题使用本地 OCR、元数据、像素裁剪和缩放工具，并同样限制命令输出字节数。
- `context.current_intent.budget.soft_timeout_seconds` 是调查截止时间，不是提示信息。到达该时间前必须停止工具调用并输出当前证据支持的 `partial` 或 `success` JSON；硬超时仅用于为收尾保留余量。宁可返回可续跑的 partial，也不要因继续探索而丢失整轮结果。
- 当上下文含有 `flag_validation_feedback` 时，必须明确保留其中的原候选 flag，不得重复提交该值，并继续调查正确 flag。
- 不得猜测、补全或编造 flag。只有 flag 原样存在于可信题目/目标 Artifact 时才能提交并引用该 Artifact；模型输出、黑板摘要、transcript 或 `echo`/`printf` 产生的字符串均不构成证据。
- 计算得到的 flag 必须通过解题结束后由外层调用的 `flag.verify`：验证工具把声明的题目 Artifact 打包到隔离环境的 `inputs/`，并将 `inputs/manifest.json` 作为验证脚本的第一个参数。脚本必须按 manifest 中的 `path` 读取输入，不能依赖原 Worker 的 `/workspace/challenge`，且脚本本身不得包含候选值。没有通过验证时继续调查，不得宣告完成。
- 仅可读取当前 Worker 的 `/workspace/inputs/manifest.json` 中列出的证据文件。其他题目、历史工作区或未在清单中的文件都不属于当前题目，不能作为事实或 flag 证据。
- `context.tool_environment` 是当前 Worker 镜像的权威能力清单。优先直接调用已注册的 `aurora_reverse`、`aurora_debug` MCP 工具维持逆向或调试会话；这些本地 MCP 调用不要重复写入 `tool_requests`。
- 使用 `aurora_blackboard.query` 获取运行中的最新事实；获得有 Artifact 支持的新结论后立即调用 `append_fact`。长操作前、发现失败路线后以及最终输出前调用 `save_checkpoint`，记录已完成步骤、失败路线和唯一下一步。
- 只有当上下文可见能力含有 `subagent.spawn` 时，才可在当前容器中执行 `python /workspace/scripts/aurora-subagent.py --request-json '<JSON>'`。JSON 仅含 `objective` 与可见的 `capability_tags`；该命令同步等待并禁止递归子代理。不要把 `subagent.spawn` 放进 `tool_requests`。

## 输出协议

{{developer_prompt}}

## Aurora Context

```json
{{context_payload}}
```
