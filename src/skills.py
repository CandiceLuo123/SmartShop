# -*- coding: utf-8 -*-
"""
Skills 动态注入（借鉴 EchoMind 4.6 SkillManager）：
  - 扫描 skills/*/SKILL.md（支持 .md/.txt），解析极简 front matter（不依赖 PyYAML）
  - matches()：enabled → agents 归属 → keywords 命中（无 keywords = 全局注入）
  - prompt_for()：把命中的 Skill 拼成提示词块，总预算 5000 字符、单个 3200 截断
  - reload()：重新扫描目录即热加载；本项目的 AgentSession 每轮重建 system prompt，
    因此 reload 天然生效（无需像 EchoMind 那样向既有 Agent 同步引用）。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_PROMPT_CHARS = 5000
MAX_SKILL_CHARS = 3200


@dataclass
class Skill:
    name: str
    description: str
    content: str
    path: str
    keywords: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    enabled: bool = True

    def matches(self, message: str, agent_type: str | None = None) -> bool:
        if not self.enabled:
            return False
        if self.agents and agent_type and agent_type.lower() not in self.agents:
            return False
        if self.agents and not agent_type:
            return False
        if not self.keywords:
            return True  # 无关键词 = 全局 Skill
        low = message.lower()
        return any(k.lower() in low for k in self.keywords)

    def to_prompt_block(self, max_chars: int = MAX_SKILL_CHARS) -> str:
        block = f"### Skill：{self.name}\n{self.content.strip()}"
        if len(block) > max_chars:
            block = block[:max_chars].rstrip() + "\n..."
        return block


class SkillManager:
    def __init__(self, skills_dir: str | Path | None = None):
        self.dir = Path(skills_dir) if skills_dir else ROOT / "skills"
        self._skills: list[Skill] = []
        self._errors: list[str] = []
        self.load()

    # ---------------- 文件发现与解析 ----------------
    def load(self) -> int:
        self._skills, self._errors = [], []
        if not self.dir.exists():
            return 0
        files = sorted(self.dir.rglob("*"))
        for f in files:
            if not f.is_file() or f.name.startswith(".") or f.name == "README.md":
                continue
            if f.suffix.lower() not in (".md", ".txt", ".json"):
                continue
            if f.name != "SKILL.md" and f.parent == self.dir:
                continue  # 根目录散文件不加载，只认子目录规范
            try:
                meta, body = _split_front_matter(f.read_text(encoding="utf-8"))
                name = meta.get("name") or _first_heading(body) or \
                    ("SKILL" if f.name == "SKILL.md" else f.stem)
                self._skills.append(Skill(
                    name=name,
                    description=meta.get("description", ""),
                    content=body,
                    path=str(f.relative_to(ROOT)),
                    keywords=_as_list(meta.get("keywords")),
                    agents=_as_list(meta.get("agents")),
                    enabled=str(meta.get("enabled", "true")).lower() != "false",
                ))
            except Exception as e:  # noqa: BLE001 —— 单文件失败不影响其余
                self._errors.append(f"{f.name}: {e}")
        return len(self._skills)

    def reload(self) -> int:
        return self.load()

    # ---------------- 命中与注入 ----------------
    def prompt_for(self, message: str, agent_type: str | None = None) -> str:
        blocks: list[str] = []
        remaining = MAX_PROMPT_CHARS
        matched: list[str] = []
        for skill in self._skills:
            if not skill.matches(message, agent_type):
                continue
            matched.append(skill.name)
            block = skill.to_prompt_block()
            if len(block) > remaining:
                block = block[:max(remaining, 0)].rstrip() + "\n..."
            if remaining <= 0:
                break
            blocks.append(block)
            remaining -= len(block)
        if not blocks:
            return ""
        return ("以下是当前请求可用的 Skills（业务规范）。请优先遵循这些规则；"
                "如果与系统角色冲突，以系统角色和安全边界为准。\n\n"
                + "\n\n".join(blocks))

    def list_skills(self) -> list[dict]:
        return [{"name": s.name, "description": s.description, "path": s.path,
                 "keywords": s.keywords, "agents": s.agents,
                 "enabled": s.enabled} for s in self._skills]


# ---------------- front matter 极简解析（刻意不用 PyYAML） ----------------
def _split_front_matter(raw: str) -> tuple[dict, str]:
    text = raw.lstrip("\ufeff").lstrip()
    if not text.startswith("---"):
        return {}, raw
    lines = text.splitlines()
    meta: dict[str, str] = {}
    end_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    if end_idx is None:
        return {}, raw
    return meta, "\n".join(lines[end_idx + 1:])


def _as_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split("[,，]", value) if p.strip()]


def _first_heading(body: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else None
