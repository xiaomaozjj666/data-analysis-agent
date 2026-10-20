"""沙箱子进程入口：读入 DataFrame 副本，执行已审查代码，把结果写成 JSON。"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

from data_agent.sandbox_runtime import run_user_code
from data_agent.serialization import json_text


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("usage: sandbox_worker CODE_PATH FRAME_PATH OUT_PATH", file=sys.stderr)
        return 2
    code_path, frame_path, out_path = (Path(item) for item in args)
    try:
        code = code_path.read_text(encoding="utf-8")
        with frame_path.open("rb") as handle:
            frame = pickle.load(handle)  # noqa: S301 — 只读父进程刚写入的 DataFrame 副本
        payload = run_user_code(code, frame)
        out_path.write_text(json_text(payload), encoding="utf-8")
        return 0
    except BaseException as exc:  # noqa: BLE001 — 原样交给父进程翻译
        out_path.write_text(
            json.dumps(
                {
                    "status": "error",
                    "exc_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
