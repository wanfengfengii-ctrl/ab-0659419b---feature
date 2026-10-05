# 计量校准换算服务 (Calibration Translation Service)

在不同仪器坐标系之间沿有向标定关系精确换算读数。所有系数使用有理数
（`fractions.Fraction`）精确表示与组合，**不使用浮点数**，因此任何两条
路径导出的变换若不完全一致，都会被当作矛盾标定拒绝，而不会被舍入掩盖。

## 接口

`POST /api/calibrations/translate`

请求字段：

- `relations`：1–100 条有向关系，每条含 `from`、`to`、`a`、`b`，
  表示 `y = a*x + b`；`a` 必须非零；`a`、`b` 接受整数或 `p/q` 字符串
  （如 `"2"`、`"-1/6"`、`"1/1000000000000000000000000000001"`），不接受
  浮点字面量。关系涉及的唯一仪器数必须在 2–50 之间。
- `source` / `target`：源、目标仪器（必须出现在关系中）。
- `readings`：至少一个待换算读数，格式同 `a`/`b`。
- `resilience`（可选）：设为 `"single_relation"` 时启用单关系冗余核验。
  启用后每条关系必须携带唯一非空的 `relationId`；服务在通过既有的精确
  一致性与可达性裁决后，再逐一排除单条关系，核对源到目标仍可达（平行
  关系按各自编号计作独立证据）。全部情形可达才返回原系数、换算值并附
  `"resilience": {"verified": true}`。省略该字段时契约与原版完全一致：
  不要求 `relationId`，响应也不含 `resilience`。

成功返回 `200`：

```json
{
  "source": "A", "target": "C",
  "coefficients": {"a": "1", "b": "5/6"},
  "readings": ["0", "1", "5/2"],
  "results":  ["5/6", "11/6", "10/3"]
}
```

错误均为 JSON：`{"error": {"code": ..., "message": ...}}`

| HTTP | code                 | 含义 |
|------|----------------------|------|
| 400  | `invalid_request`    | 参数/格式非法（含 `a=0`、仪器或关系数越界、启用 resilience 时 `relationId` 缺失/空白/重复） |
| 404  | `unreachable`        | 源到目标无连通路径（含 `source`、`target`） |
| 409  | `conflict`           | 某条环路闭合时与已有精确变换不一致；`cycle` 列出涉及仪器，且不返回任何换算结果 |
| 422  | `resilience_not_met` | 一致且可达，但排除某条关系后源目标断开；`criticalRelationIds` 按编号排序列出全部关键关系，且不返回系数或换算值 |

健康检查：`GET /health` → `200 {"status":"ok"}`。

## 一致性保证

遍历以源为根的连通分量：每个节点记录「源→该仪器」的精确仿射变换。
每条无向边（含逆关系）闭合到已访问节点时，都要求推出的变换与记录的
变换**逐系数相等**；否则沿生成树构造基本环并报 `conflict`。邻接按仪器
名排序后遍历，故答案与关系录入顺序无关（测试覆盖三种不同录入顺序）。

## 冗余核验（resilience）

`resilience="single_relation"` 的裁决顺序固定为：先做全图的精确一致性
（409）与可达性（404）裁决——矛盾或不可达优先返回，不再进入冗余核对；
随后对每条关系分别构造「移除该关系」的残图并核对源、目标连通性。残图
是全图的子图，一致性天然继承，故只需核对可达性。任一关系被移除后源
目标断开即判为关键关系：全部关键关系的编号排序后以 422
`resilience_not_met` 返回，且响应不含系数与换算值；全部可达才返回与
未启用时完全相同的系数和换算值，并附 `resilience.verified=true`。

## 运行

仅依赖 Python 3.11 标准库。

```bash
# 启动 API（宿主机端口可用 HOST_PORT 覆盖，默认 8000）
HOST_PORT=9000 docker compose up -d --build api

# 一次性校验服务：等待 API 健康后运行单元测试、构建检查
# （compileall）以及一致链/矛盾链/不可达/冗余核验的接口冒烟，
# 以退出码报告
docker compose up --build verify
docker inspect --format '{{.State.ExitCode}}' \
  $(docker compose ps -q verify)   # 0 表示全部通过
```

本地直接运行：

```bash
python -m unittest discover -s tests -v
PORT=8000 python -m app.server
python scripts/verify.py           # 对已运行的服务做一次性校验
```
