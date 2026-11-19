"""Read-only Agent Skills tools; descriptions are discovered before bodies."""
from pydantic import Field

from ..config import StrictModel
from ..data.contracts import Observation
from ..errors import AgentError
from ..skill_library import NAME_PATTERN, READ_SKILL_TOOLS, SkillLibrary
from .registry import Tool


class ReadSkill(StrictModel):
    name: str = Field(min_length=1, max_length=64, pattern=f"^{NAME_PATTERN}$")


class ReadSkillResource(ReadSkill):
    path: str = Field(min_length=1, max_length=300)
    offset: int = Field(0, ge=0, strict=True)
    limit: int = Field(6000, ge=1, le=12000, strict=True)


def register_skill_tools(registry, names=None, *, library=None):
    library = library or registry.skill_library or SkillLibrary()
    library = library if names is None else library.view(names)
    registry.skill_library = library

    def read(args, ctx):
        body = library.read(args.name)
        ctx.loaded_skills[args.name] = body["sha256"]
        return Observation(status="ok", source="packaged_skill", data=body)

    def resource(args, ctx):
        if args.name not in ctx.loaded_skills:
            raise AgentError("SKILL_NOT_LOADED", "先读取技能正文，再按其中说明读取所需资源")
        return Observation(status="ok", source="packaged_skill_resource",
                           data=library.read_resource(args.name, args.path, args.offset, args.limit))

    # Rebind closures when narrowing a role's catalog; filtering metadata alone
    # must not leave a handler that can read hidden skills.
    for name in READ_SKILL_TOOLS:
        registry.tools.pop(name, None)
    registry.register(Tool("read_skill", "按available_skills中的唯一name读取技能正文。仅在任务适用或用户点名时调用；不改变权限，不是金融证据。", ReadSkill, read, False))
    registry.register(Tool("read_skill_resource", "读取已加载技能包内的相关文本参考；path使用正文中的相对路径，支持分页，不执行脚本。", ReadSkillResource, resource, False))
