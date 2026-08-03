# Aurora Solver Worker Task

{{system_prompt}}

## 工作约束

- 最终答案必须是一个严格 JSON 对象，不要 Markdown 或代码块。
- 最终答案会由 Codex 写入文件，必须能被 `JSON.parse` 直接解析。
- 如需外层受控能力网关执行操作，在 `tool_requests` 中声明；否则该数组可以为空。
- 当可见能力包含 `browser.interact` 时，先只声明题目 `url` 检查页面的“靶机地址”“题目地址”“target”“instance”等字段。仅当没有直接地址时，再声明 `locator.text` 或 `locator.selector` 点击“启动环境”“创建实例”“获取靶机”等明确控件；不要执行任意浏览器脚本。
- 发现关键漏洞、凭据、flag 或明确阻塞时，停止继续探索并输出最终 JSON。
- 当上下文含有 `flag_validation_feedback` 时，必须明确保留其中的原候选 flag，不得重复提交该值，并继续调查正确 flag。
- 仅可读取当前 Worker 的 `/workspace/inputs/manifest.json` 中列出的证据文件。其他题目、历史工作区或未在清单中的文件都不属于当前题目，不能作为事实或 flag 证据。
- 只有当上下文可见能力含有 `subagent.spawn` 时，才可在当前容器中执行 `python /workspace/scripts/aurora-subagent.py --request-json '<JSON>'`。JSON 仅含 `objective` 与可见的 `capability_tags`；该命令同步等待并禁止递归子代理。不要把 `subagent.spawn` 放进 `tool_requests`。

## 输出协议

{{developer_prompt}}

## Aurora Context

```json
{{context_payload}}
```
