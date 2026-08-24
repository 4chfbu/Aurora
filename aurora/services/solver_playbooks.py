from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SolverPlaybook:
    challenge_type: str
    confidence: float
    first_steps: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    capabilities: tuple[str, ...]


_PLAYBOOKS: dict[str, SolverPlaybook] = {
    "web": SolverPlaybook("web", 1.0, ("确认授权目标和入口", "做一次低噪声指纹/路由枚举", "根据响应选择一个可证伪漏洞假设"), ("目标连续返回平台错误且非输入相关", "连续两轮没有新增权限或可复现证据"), ("http.request", "blackboard.query")),
    "reverse": SolverPlaybook("reverse", 1.0, ("用 triage_binary 建立文件/保护/入口基线", "用字符串交叉引用定位校验路径", "用专用逆向或调试工具验证一条输入路径"), ("已确认是输入访问码但尚未证明程序输出凭证", "两轮静态搜索没有缩小校验路径"), ("binary.inspect", "sandbox.exec")),
    "pwn": SolverPlaybook("pwn", 1.0, ("确认架构和 mitigations", "定位第一个可控输入", "用 Pwndbg 高信号命令和最小 PoC 验证崩溃或信息泄露"), ("目标不可达或 PoC 不可复现", "连续两轮只改变 payload 外形"), ("binary.inspect", "sandbox.exec")),
    "crypto": SolverPlaybook("crypto", 1.0, ("保留原始样本并计算摘要", "识别编码/密钥/代数结构", "写可重放脚本验证唯一候选"), ("候选只来自格式猜测", "连续两轮未缩小候选空间"), ("python.analyze", "sandbox.exec")),
    "forensics": SolverPlaybook("forensics", 1.0, ("保存哈希和文件类型", "提取元数据和嵌入内容", "对最高信号线索做一次定向验证"), ("重复扫描没有新增 Artifact", "证据文件不在当前 manifest"), ("forensic.inspect", "sandbox.exec")),
    "misc": SolverPlaybook("misc", 1.0, ("列出附件和可执行入口", "按题面选择一个最小实验", "记录可复现输出并验证候选"), ("实验结果与题目输入无关", "连续两轮没有新增事实"), ("blackboard.query", "sandbox.exec")),
}

_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("web", "web"), ("网站", "web"), ("http", "web"), ("api", "web"), ("ssrf", "web"), ("sql", "web"),
    ("reverse", "reverse"), ("逆向", "reverse"), ("elf", "reverse"), ("binary", "reverse"), ("exe", "reverse"), ("apk", "reverse"),
    ("pwn", "pwn"), ("栈溢出", "pwn"), ("heap", "pwn"), ("rop", "pwn"),
    ("crypto", "crypto"), ("密码", "crypto"), ("加密", "crypto"), ("rsa", "crypto"), ("aes", "crypto"),
    ("forensic", "forensics"), ("取证", "forensics"), ("memory dump", "forensics"), ("pcap", "forensics"),
)

_ALIASES = {
    "webapp": "web", "web_app": "web", "web application": "web",
    "rev": "reverse", "re": "reverse", "forensic": "forensics",
}


def select_playbook(challenge_type: str | None, evidence_text: str = "") -> SolverPlaybook:
    explicit = _ALIASES.get((challenge_type or "").strip().lower(), (challenge_type or "").strip().lower())
    if explicit in _PLAYBOOKS:
        return _PLAYBOOKS[explicit]
    haystack = evidence_text.lower()
    for keyword, kind in _KEYWORDS:
        if keyword in haystack:
            base = _PLAYBOOKS[kind]
            return SolverPlaybook(kind, 0.65, base.first_steps, base.stop_conditions, base.capabilities)
    base = _PLAYBOOKS["misc"]
    return SolverPlaybook("unknown", 0.0, base.first_steps, base.stop_conditions, base.capabilities)
