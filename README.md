# 资料包整理（archive-collect-organize）

把一堆压缩包和散乱文件夹，整理成能直接交出去的资料包：解压补缺、撞名不覆盖、按修改时间取新、旧版归档、全程出报告。

## 解决的问题

- 多个压缩包与已有目录大量重叠，分不清哪些是真缺的
- 同名文件内容不同（定稿 vs 备份、盖章 vs 未盖章），手工比对容易覆盖丢证据
- 解压出来的文件时间戳全变成解压时刻，没法判断新旧
- 整理完说不出「哪些文件是新补的、哪些被替换了」

## 依赖

Python 3.8+，只用标准库，不需要安装任何包。

## 安装

把本目录放到技能目录下即可：

```
C:\Users\<用户>\.workbuddy\skills\archive-collect-organize\
```

## 用法

```bash
# 预演：只出报告，不动文件
python scripts/archive_sync.py --root "D:\资料包" --config config/mapping.example.json

# 执行补缺（只增不改）
python scripts/archive_sync.py --root "D:\资料包" --config config/mapping.example.json --apply

# 冲突裁决：按修改时间取新，旧版进 _旧版本
python scripts/resolve_conflicts.py --root "D:\资料包"            # 预演，出 decisions.csv
python scripts/resolve_conflicts.py --root "D:\资料包" --apply    # 执行
```

## 输出

- `_整理报告\archive_sync.json`：每个包的补缺、跳过、冲突、失败明细
- `_整理报告\decisions.csv`：冲突对的双方修改时间、保留哪份、做了什么（Excel 直接打开）
- `_旧版本\`：被判为旧的那一份，与正本同目录

## 设计取舍

**为什么按压缩包内时间判断新旧**：文件系统时间会被解压、复制、移动刷新，不可信；压缩包条目里的时间在打包那一刻写入，解压不会改动它。两个时间一比，就是打包时两份文件的真实先后。

**为什么撞名不覆盖**：同名不同内容多为定稿与备份、盖章与未盖章。程序判断不了哪份有法律效力，覆盖会丢证据，所以两份都留，旧的进 `_旧版本\`，随时能反向还原。

**为什么只补缺**：补缺是无损操作，可重复执行；覆盖和删除是有损操作，一次误执行就不可逆。

## 测试

```bash
python -m unittest discover -s tests -v
```

9 项用例覆盖：同内容跳过、撞名生成补文件、大小写差异不覆盖、预演不落盘、包内较新替换并归档、现有较新归档补文件、时间相同不动、CSV 编码、无报告时回退扫描。

## 已知限制

- 不判断文件内容的语义（不读合同条款）
- 现有文件若在入包后被复制过，其时间会被刷新，可能判成「现有更新」，此时旧版仍保留在 `_旧版本\`，可人工纠正
- 加密包、分卷包不支持
- 目录结构剧烈变化时，分类映射需要人工调整 `config/mapping.example.json`

## 许可

AGPL-3.0，见 LICENSE。
