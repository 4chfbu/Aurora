# 跨会话上下文与继承优化

本次比较的“原版”是 **2026-09-20 开始修改前的当前工作区**，包括当时已有的未提交修改，不是 Git HEAD。源码基线和 SHA-256 清单保存在 `.runtime-cache/context-inheritance-20260920/baseline/` 与 `baseline-manifest.json`。本次没有启动真实模型、比赛任务或平台提交。

## 改了什么

| 环节 | 修改前 | 修改后 |
| --- | --- | --- |
| 分支交接 | 启动快照和实时黑板主要按全项目最近记录选取；显式续跑仅覆盖部分入口 | 共享父 Attempt 选择逻辑；当前分支的 checkpoint、事实及实时交接优先，沿父 Attempt 保留最多 4 层交接，再补充近期同伴观察 |
| checkpoint 继承 | `parent_checkpoint_id` 指向项目中最新的 checkpoint，可能来自其他分支 | 指向当前 Attempt 实际继承分支的 checkpoint |
| 上下文大小 | 超限后固定保留前 10 条事实、5 条 Artifact 摘要，未确认实际大小 | 按 UTF-8 字节确认上限；保留必需事实引用，缩短过长正文、减少可选历史；完整的本轮压缩前上下文归档并提供读取路径 |
| 恢复文件 | 工作文件归档，但 Codex 会话文件只记哈希，恢复依赖旧 Worker 目录 | v2 清单把会话文件也保存为按内容去重的 Artifact；恢复前验证全部文件，随后发布，发布失败会回滚 |
| 旧输入 | 不在新 Worker 最近输入窗口中的文件会使恢复失败 | 按原清单从同项目 Artifact 恢复原路径，并合并新旧输入清单；父分支事实引用的证据也固定保留 |
| 降级与环境变化 | 会话恢复失败时可能清空新工作目录；实例变化未阻止历史 thread 续用 | 验证失败保留新工作目录；会话损坏或实例变化时启动新 thread，仍可恢复验证通过的脚本和输入；交接信息标记需要重验 |
| 恢复回退后的上下文 | 原提示词可能仍描述首选父 Attempt，而运行时已经恢复另一个父 Attempt | 先确认实际恢复来源，再刷新同一 ContextSnapshot 并生成提示词 |
| 提示词冗余 | 工具列表和输出协议同时出现在开发者提示词与上下文 JSON 中 | 保留开发者提示词中的唯一工具契约和输出协议，上下文 JSON 不再重复，使用紧凑 JSON |

主要代码位于 `context_memory.py`、`context_budget.py`、`resume_store.py`；`context_builder.py`、`worker_runtime.py`、`worker_control.py`、`demo.py` 和 `round_summary.py` 使用这些公共逻辑。没有新增数据库字段，也不需要迁移数据库。

## 相同场景的原版/修改版比较

使用同一份脚本、相同配置、内存 SQLite 和临时目录，分别加载保存的原版源码与当前源码。原始结果见 [context-inheritance-comparison-20260920.json](context-inheritance-comparison-20260920.json)。

| 检查 | 原版 | 修改版 |
| --- | ---: | ---: |
| 长中文题目、12 条必需事实，快照预算 16,000 字节：实际快照 | 372,388 字节，超限 | 12,607 字节，满足上限 |
| 上述场景保留的必需事实 | 10/12 | 12/12 |
| 本轮未压缩内容可另行读取 | 否 | 是 |
| 上述场景完整渲染提示词 | 398,567 字节 | 32,191 字节 |
| 60 条同伴记录之后，父分支 checkpoint 仍优先 | 否 | 是 |
| 同一场景中父分支事实仍在启动上下文中 | 否 | 是 |
| 同一场景中实时黑板仍优先展示父分支交接 | 否 | 是 |
| 删除旧 Worker 目录、且新 Worker 没有旧输入后，恢复 thread | 失败 | 成功 |
| 上述恢复场景中的脚本和旧输入 | 未恢复 | 均恢复 |

这个长上下文边界场景中，快照字节数减少约 96.6%，完整提示词字节数减少约 91.9%。**这是确定性场景的内容体积比较，不是模型实际 token 计费、解题成功率或线上延迟的 A/B 结果。** `max_context_snapshot_bytes` 限制快照正文，不包含静态系统提示词和工具协议，因此完整提示词可以大于快照上限。

复现命令：

```bash
.venv/bin/python scripts/compare-context-inheritance.py \
  --baseline .runtime-cache/context-inheritance-20260920/baseline \
  --output .runtime-cache/context-inheritance-20260920/comparison.json
```

## 验证与边界

- 初始 9 个定向场景在修改前为 **1 通过、8 失败**；这 8 个失败涵盖分支上下文、实时黑板、checkpoint 父链、上下文超限、旧目录清理、旧输入恢复、恢复失败保护和环境变化。
- 最终新增测试共 **15 项**，另覆盖损坏会话的工作文件降级、实际回退来源与提示词一致性、跨项目文件拒绝、父分支证据保留、实例记录过期，以及恢复文件发布中途失败的回滚；全部通过。
- 完整后端回归：**452 passed**，仅有已有的 Starlette/httpx 弃用警告。
- 恢复清单 v1 仍可读取；清理旧目录后可独立恢复的能力适用于新写出的 v2 清单，无法补救已经丢失的旧版会话文件。
- 归档会增加磁盘占用；未改变的工作文件和会话文件会复用同项目、同内容的 Artifact。Codex 状态文件的归档字节数也受 `AURORA_RESUME_MAX_BYTES` 限制。
- 实例改变后保留的旧输入仅用于历史分析，仍标注证据时效；不会把旧实例的证据自动变为当前有效证据。Flag 验证规则保持原有校验。
- `context_memory` 是本轮选中上下文的完整归档，属于 `runtime_state`，不能作为独立的 Flag 证据；更早、未被选入本轮的项目记录仍保留在数据库和 Artifact Store。

本次变更相对原工作区的补丁保存在 `.runtime-cache/context-inheritance-20260920/changes.patch`，不包含开始前已存在的其他修改。
