---
name: binary-triage
description: 高信号二进制题解流程：先建立 ELF/PE 基线，再用字符串交叉引用定位路径，最后用最小动态实验验证一个假设。
---

# Binary Triage

这是逆向、Pwn 和带本地附件题目的固定首轮流程。每轮只验证一个假设，并把原始输出保存到 `/workspace/work`。

## 固定顺序

1. 对样本执行 `triage_binary`，确认文件类型、架构、入口、解释器、RELRO/Canary/NX/PIE 和 SHA-256。
2. 用 `aurora_reverse.open_binary` 打开样本；先 `list_functions`，再用 `list_strings` 搜索 `flag`、`pass`、`key`、`success`、`fail`、协议错误和用户提示。
3. 对命中的字符串调用 `find_string_xrefs`，只跟进最少的调用点；不要对整个函数表逐个反编译。
4. 对入口、校验函数和字符串调用点调用 `decompile_function`；反编译失败时保留 Rizin 汇编，不要反复切换引擎。
5. 只有静态证据无法区分假设时才启动 `aurora_debug`。Pwn 题优先通过 `pwndbg_command` 使用 `checksec`、`vmmap`、`telescope`、`search`、`got`、`canary`、`cyclic`、`nextcall`、`nextjmp`、`nextret`；没有 Pwndbg 时使用等价 GDB 命令。
6. 动态实验必须是最小可复现输入，记录断点、寄存器/栈观察和退出结果；不要把“程序能运行”当成漏洞证据。

## 证据与止损

- 先保存文件摘要和命令输出，再写结论；结论必须引用 Artifact。
- 输入访问码、解码中间值和程序最终输出凭证是不同对象，必须分别证明。
- 连续两轮没有新增调用点、权限、可复现崩溃或候选空间收敛时，保存 checkpoint 并换假设。
- 不要从公开仓库、常见 flag 格式、大小写/前后缀变体猜测候选。
