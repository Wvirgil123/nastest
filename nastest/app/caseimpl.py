"""用例实现聚合入口。

executor 从这里导入 IMPL / Ctx / CaseAbort。
具体实现按域拆分在 impl_env(S0-S2)/ impl_stor(S3-S4)/
impl_proto(S5-S6)/ impl_app(S7-S9)。
"""
from .ctx import CaseAbort, Ctx  # noqa: F401 (executor 从此处导入)
from . import impl_app, impl_env, impl_proto, impl_stor

IMPL: dict[str, callable] = {}
for _m in (impl_env, impl_stor, impl_proto, impl_app):
    IMPL.update(_m.IMPL)
