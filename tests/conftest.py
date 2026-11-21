"""测试环境：避免导入 app.main 时把仓库里自带的 data/freshness.db 就地迁移。"""
from __future__ import annotations

import os
import tempfile

# app.main 在模块导入时会 create_app()，默认使用 cwd/data/freshness.db；
# 测试统一改到临时文件，保持仓库内的旧库样本不变。
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="freshness-tests-"), "import.db")
os.environ["DB_PATH"] = _TEST_DB
