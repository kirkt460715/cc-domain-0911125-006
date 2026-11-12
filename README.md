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
