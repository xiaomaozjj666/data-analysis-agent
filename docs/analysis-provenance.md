# 分析溯源：报告背后的证据链

分析报告里的每个数字，最终都能追到一次工具调用、一份输入数据和一组产物文件。
`provenance.jsonl` 把这层关系固化成哈希链，事后可以离线复核「报告有没有被改过、
数据是不是当时那份」。

## 记录了什么

每次分析完成后，服务端在会话目录写入两个文件：

```
runs/<session_id>/
  provenance.jsonl   # 逐条记录，哈希链
  provenance.json    # 摘要：条目数、根哈希、模型、代码版本
```

`provenance.jsonl` 每行一个 JSON 对象：

```json
{"seq":0,"type":"meta","name":"analysis","data":{...},"prev":"","hash":"..."}
{"seq":1,"type":"dataset","name":"sales.csv","data":{"path":"input/sales.csv","sha256":"...","size":2048},"prev":"<上一条 hash>","hash":"..."}
{"seq":2,"type":"tool","name":"run_sql","data":{"kind":"tool_call","detail":"SELECT ...","detail_sha256":"...","chars":42},"prev":"...","hash":"..."}
{"seq":3,"type":"artifact","name":"chart.html","data":{"path":"artifacts/chart.html","sha256":"...","size":8192},"prev":"...","hash":"..."}
{"seq":4,"type":"report","name":"analysis-report","data":{"sha256":"...","chars":1234},"prev":"...","hash":"..."}
```

各类型的 `data`：

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `meta` | `task_sha256` / `task_chars` / `provider` / `model` / `code_version` / `created_at` | 任务原文只存哈希与长度，不落盘 |
| `dataset` | `sha256` / `size` / `path` 或 `external` | 分析实际载入的数据文件 |
| `tool` | `kind` / `detail` / `detail_sha256` / `chars` | `trace` 里的工具调用与结果，逐条入链 |
| `artifact` | `kind` / `description` / `path` 或 `external` / `sha256` / `size` | 图表、导出数据等产物 |
| `report` | `sha256` / `chars` | 最终 Markdown 报告正文 |

哈希算法（与 `src/data_agent/provenance.py` 一致）：

```
body  = 除 hash 外的条目字段
hash  = sha256( json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) )
```

`prev` 指向上一条的 `hash`，首条为空串。摘要里的 `root_hash` 等于链尾 `hash`。
改任何一条的任何一个字段，该条 `hash` 与后续 `prev` 都会对不上。

## 复核

服务端复核会做四件事：

1. 重算每条 `hash`，并检查 `prev` 是否等于上一条 `hash`；
2. 对照 `provenance.json`：条目数与根哈希必须一致；
3. 重新计算数据集与产物文件的 sha256，报告缺失或哈希不匹配；
4. 读取 `session.json` 的 `last_result.response`，与 `report` 条目比对。

API：

```bash
# 摘要 + 当场复核（200；verification.ok 表示是否通过）
curl -H "Authorization: Bearer $APP_ACCESS_TOKEN" \
  http://127.0.0.1:8000/api/sessions/<session_id>/provenance

# 自包含 JSON 下载（摘要 + 全部条目 + 复核结果）
curl -OJ -H "Authorization: Bearer $APP_ACCESS_TOKEN" \
  http://127.0.0.1:8000/api/sessions/<session_id>/provenance/export
```

界面入口：报告右上角「导出 → 分析溯源 (.json)」。

拿到导出文件后可以离线复核，不需要信任服务端的结论：

```python
import hashlib, json

def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

doc = json.load(open("分析溯源_2026-10-25.json", encoding="utf-8"))
prev = ""
for entry in doc["entries"]:
    body = {key: value for key, value in entry.items() if key != "hash"}
    digest = hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
    assert entry["prev"] == prev and entry["hash"] == digest, entry["seq"]
    prev = entry["hash"]
assert prev == doc["summary"]["root_hash"]
```

文件本身仍在原目录时，再对 `data.path` 指向的文件重算 sha256 即可确认字节未变。

## 边界

- **哈希链不是签名。** 它防静默漂移、手滑编辑和传输损坏，不防有写权限的对手：
  能重写整条链的人也能重算根哈希。需要抗抵赖时，把导出的 `root_hash` 记到
  外部系统（工单、发布记录、`data-agent-eval` 的 ledger）再比对。
- **只保留最近一次分析。** 与 `session.json` 的 `last_result` 对齐；重跑会覆盖
  上一次的溯源文件。
- **写入是尽力而为。** 溯源失败只记服务端日志，不会把一次成功的分析变成 500；
  此时 `GET /provenance` 返回 404，明确表示“没有记录”，而不是给出错误的通过结论。
- **外部数据集**（`adopt` 进来的 `dataset_path` 在工作区之外）只记录当时哈希，
  复核时按绝对路径读取；文件移走后会报告缺失。
- 报告正文与 `session.json` 的比对只在后者存在时进行；对比的是文本哈希，
  不做语义等价判断。

## 与 evalkit 的关系

`data-agent-eval`（见 [agent-eval.md](agent-eval.md)）管的是「改版前后谁更好」，
它的 ledger 记录裁决；溯源管的是「这一份报告怎么来的」。两者都是哈希链，但
作用对象不同：evalkit 记录多次运行的选择，provenance 记录一次运行的证据。
