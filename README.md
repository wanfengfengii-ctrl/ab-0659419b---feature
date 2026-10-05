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
- `resilience`（可选）：放行前的证据链冗余裁决，目前仅支持
  `"single_relation"`。省略时沿用原契约。启用后每条关系必须带唯一、
  非空的 `relationId`；服务先按原规则裁决精确一致性与可达性，再逐条
  排除单条关系（连同其逆方向）核对源和目标是否仍连通。平行关系各有
  独立编号，计作彼此独立的证据链。全部情形仍可达时，在原响应上追加
  `"resilience": {"mode": "single_relation", "verified": true}`，并照常
  返回系数与换算值。

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

| HTTP | code              | 含义 |
|------|-------------------|------|
| 400  | `invalid_request` | 参数/格式非法（含 `a=0`、仪器或关系数越界、启用 resilience 时 `relationId` 缺失/为空/重复、未知 `resilience` 值） |
| 404  | `unreachable`     | 源到目标无连通路径（含 `source`、`target`） |
| 409  | `conflict`        | 某条环路闭合时与已有精确变换不一致；`cycle` 列出涉及仪器，且不返回任何换算结果 |
| 422  | `resilience_not_met` | 仅冗余不足：图本身一致且可达，但撤销某条关系后源无法到达目标。`criticalRelationIds` 按编号排序列出所有这类关键关系；**不返回系数或换算值**。原网络矛盾或不可达仍分别返回 409/404，优先于本裁决 |

健康检查：`GET /health` → `200 {"status":"ok"}`。

## 一致性保证

遍历以源为根的连通分量：每个节点记录「源→该仪器」的精确仿射变换。
每条无向边（含逆关系）闭合到已访问节点时，都要求推出的变换与记录的
变换**逐系数相等**；否则沿生成树构造基本环并报 `conflict`。邻接按仪器
名排序后遍历，故答案与关系录入顺序无关（测试覆盖三种不同录入顺序）。

## 单关系失效冗余（resilience）

放行关键换算前，必须确认任一单条标定关系（含其证书）被撤销后，仍存在
独立证据链完成换算。请求带 `"resilience": "single_relation"` 且每条关系
有唯一非空 `relationId` 时，裁决分两阶段：

1. 沿用上述精确一致性与可达性裁决——任何矛盾仍返回 409，任何不可达
   仍返回 404，优先于冗余裁决；
2. 逐一排除每条关系（连同其逆边）重做连通性判断：若某次排除后源无法
   到达目标，则该关系位于所有证据链上，记入 `criticalRelationIds`。

只有全部排除情形下源仍可达目标，才返回原系数、换算值以及
`resilience.verified=true`。平行关系（同两个仪器间的多条独立标定）编号
不同，被分别排除，因此互为备份。只要存在关键关系，即返回
`422 code=resilience_not_met`，`criticalRelationIds` 按编号排序，且响应
不含 `coefficients` 或 `results`——冗余不足时不发放换算依据。

## 运行

仅依赖 Python 3.11 标准库。

```bash
# 启动 API（宿主机端口可用 HOST_PORT 覆盖，默认 8000）
HOST_PORT=9000 docker compose up -d --build api

# 一次性校验服务：等待 API 健康后运行单元测试、构建检查
# （compileall）以及一致链/矛盾链/不可达的接口冒烟，以退出码报告
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
