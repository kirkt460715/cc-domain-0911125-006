# 本地证据保管与内容保全

案件、交接和封存记录具有保管链。角色是本地操作职责；摘要和见证回执不代表外部签名。逻辑删除不承诺立即释放磁盘。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q sealvault
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m sealvault replay fixtures/sealing.json
python3 -m sealvault --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。

## 排班试算

接收与复核瓶颈可以用流程元数据做离散事件仿真，不读取证据内容，也不改动任何保管或交接状态：

```sh
python3 -m sealvault describe fixtures/simulation/night-shift.json
python3 -m sealvault simulate fixtures/simulation/night-shift.json --plan late-receive-no-review
python3 -m sealvault compare fixtures/simulation/future-surge.json --repetitions 20
```

模型输入、删失处理、同一时刻的事件顺序、跨班与返工语义、指标定义和方案比较方法见 [docs/simulation.md](docs/simulation.md)。
