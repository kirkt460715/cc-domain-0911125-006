# 排班试算：接收与复核瓶颈判断

本目录说明如何用流程元数据做离散事件仿真，只用于排班试算。仿真不读取证据内容，也不改动任何保管、封存或交接状态。

## 范围与输入

模型是 `sealvault-simulation-model` v1，由五类输入组成：

| 输入 | 含义 | 约束 |
| --- | --- | --- |
| `arrivals` | 已冻结的案件到达记录 | 历史重放只允许 `observed`；未来情景只允许 `scenario` 且必须带 `assumption_id` |
| `calendars` | 各角色工时日历 | 每个窗口必须在同一本地日结束；同一星期内窗口不可重叠；`headcount` 非负 |
| `routes` | 作业路线 | 每步绑定角色；返工只能回到更早的步骤，不能绕过前置环节 |
| `service` | 服务时长模型 | 只接受完整配对的起止记录或显式假设；等待时间不得计入操作耗时 |
| `plans` | 待比较的排班方案 | `cross_shift`（跨班持续/中断）固定；可按角色覆盖工时或人数 |

模型不含任何证据载荷：键名出现 `content`、`base64`、`bytes`、`blob`、`payload` 等即报 `sim_evidence_access`。

## 时长与删失

- `source: "paired_start_end"` 必须同时给出 `sample_count`，并在 `coverage.pairs` 中提供逐条完整的 `started_at` / `finished_at`。模型会重算中位数（固定时长）或对数正态中位数与 `sigma`，与声明不符即拒绝。
- `source: "explicit_assumption"` 必须带 `assumption_id`，并在报告里原样保留，便于复核。
- 未结束的作业写入 `coverage.open_operations`，标记 `censored: true`。它们不会被补零、不会被插补，统计上只出现在覆盖说明里。把它们悄悄丢弃从而得出"人手充足"的做法在这里被明确排除。
- `excluded_wait_seconds` 记录交接等待被排除的总量。

## 仿真语义

- **同一时刻的顺序固定**：到达 → 服务完成 → 换班边界 → 派工。报告中的 `simulation.tie_order` 始终记录这个顺序。
- **班次外不开始新作业**：派工只在有编制且窗口内的时刻发生；截止时刻同样不开始。
- **跨班行为由方案固定**：`continue` 会在跨越的每个窗口占座，窗口之间的无人时段记入 `overtime_seconds`，不计入编制内利用率；`interrupt` 在窗口末尾挂起，剩余工作量在下一个窗口恢复。
- **资源占用**：按窗口逐段登记区间，独立复核峰值并发数不超过 `headcount`，且不会为负（`schedule_integrity`）。
- **返工**：按路线规则回到指定环节，`max_rework_count` 限制次数；案件内前置关系不会被跳过，运行后做一次前置关系审计。
- **截止仍在队列中的案件进入积压分母**：`backlog_definition` 明确写出"截止时未完成的案件数 / 全部到达数，含排队与挂起"。

## 指标

每份报告给出：

- 每案的到达、首次开工、完成用时、排队等待、工作用时、返工次数、分环节明细与逐案轨迹（`cases`）；
- 各角色利用率（`occupied_headcount_seconds / scheduled_headcount_seconds`）、加班时长、截止时排队与挂起数；
- 按优先级的服务目标违约率；截止时未完成的案件按违约计；
- 人员班次数、成本、每完成案件成本，以及与 `personnel_budget` 的对比（`within_budget`）。

完成用时的均值只计算已完案件（`mean_completion_censored_excluded: true`），删失案件不做插补，只进入积压与违约统计。

排队等待分两栏：`queue_wait_seconds` 只统计真正开工过的环节；截止时仍排队或挂起的案件，其已累积等待单独记入 `censored_wait_seconds`，汇总为 `mean_censored_wait_seconds`，不会混入服务用时。

## 方案比较

`compare` 对每个方案重复多次，到达路径来自同一份冻结记录，服务与返工随机数按 `(case_id, stage_id, attempt)` 由种子键控生成，因此同一重复内不同方案面对同一批作业随机数。输出包含：

- 每个方案的均值与 95% 区间；
- 相对基线的配对差值、标准误、95% 区间与相对变化，即蒙特卡洛误差区间；
- 是否始终落在人员预算内。

区间是蒙特卡洛不确定性，不是对未观测需求的保证。

## 历史与情景分开

- `historical_replay` 只回放冻结的观察到达，`observed_window` 约束所有到达时刻，`historical_claim` 明确写出"没有用完成或交接记录反推到达时间"。
- `future_scenario` 的每条到达都带 `assumption_id`，`historical_claim` 明确写出"是情景假设，不是预测，不做准确率声明"。观察期未见的突发量只能以这种方式出现。

## 命令

```sh
python3 -m sealvault describe fixtures/simulation/night-shift.json
python3 -m sealvault simulate fixtures/simulation/night-shift.json --plan day-only
python3 -m sealvault simulate fixtures/simulation/night-shift.json --plan late-receive-no-review --case night-standard-1
python3 -m sealvault compare fixtures/simulation/future-surge.json --repetitions 20
```

## 三个示例的结论

1. **单资源可手算队列**（`fixtures/simulation/queue-single.json`）：一个接收席位，四个同刻到达、每件 15 分钟。急件 0 等待、15 分钟完成；两个普通件依次 15/30 分钟等待，30/45 分钟完成；缓件 45 分钟等待、60 分钟完成。平均完成 2250 秒、平均等待 1350 秒、利用率 3600/10800 = 0.3333、人员班次 1。加开第二席后平均完成降到 1350 秒，成本翻倍——这就是可以逐项核对基线。
2. **跨班返工**（`night-shift.json` 的 `full` 路线）：复核发现缺陷退回接收，`assume-review-rework-50pct` 是显式假设，`max_rework_count` 限制为 2；每个案件的轨迹都会显示"复核→接收→复核"的次序，且不会跳过接收直接进入交接。
3. **晚班无复核人**（`night-shift.json` 的 `late-receive-no-review`）：只把接收延长到 23:00。结果是利用率上升（接收 0.41），但服务目标违约率停在 0.75，与只开白班相同；积压没有下降，成本从 4 升到 6。把复核也排到晚班（`late-receive-and-review`）才把违约率压到 0.25，但人员班次升到 8，超出 6 的预算。这个组合正是主管要的判断：瓶颈在复核，不在接收。

## 只读保证

仿真只接受 JSON 模型文件，不打开数据库，不读对象内容，不写任何案件状态。报告里的 `summary.evidence_content_read` 恒为 `false`，`describe` 也输出同样的声明。
