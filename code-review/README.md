# Code Review Skill

自动化代码审核技能，适用于通用代码工程的定期代码质量审查。

## 快速开始

```
/code-review
```

首次运行时，如果不存在 `AiDoc/CodeReview/config.json`，会交互式询问审核参数。后续运行自动读取配置。

## 工作流程

```
阶段0 加载配置 → 阶段1 确定范围 → 阶段2 模块划分 → 阶段3 并行审查 → 阶段4 汇总报告 → 阶段5 作者追溯
```

| 阶段 | 说明 | 预计耗时 |
|------|------|----------|
| 0 | 读取 `config.json`，创建输出目录（`检查时间@开始日期@结束日期`） | < 1 min |
| 1 | `git log` 获取变更文件列表，过滤后缀/排除目录 | 5-10 min |
| 2 | 按业务模块分组，单模块不超过 `max_files_per_module` 个文件 | 5-10 min |
| 3 | 多 agent 并行审查各模块，生成 `review_*.md` | 视模块数，20+ 模块约 2-3 h |
| 4 | 合并为 `SUMMARY.md` + `FULL_REPORT.md` | 10-15 min |
| 5 | 运行 `blame_split.py` 做作者归属 + 按人拆分 | 15-30 min |

## 配置文件

路径：`AiDoc/CodeReview/config.json`

```jsonc
{
  "target": {
    "directories": ["<target-directories>"],           // 审核目标目录
    "file_extensions": [".cs"],                         // 目标文件后缀
    "exclude_directories": ["<exclude-directories>"],    // 排除目录
    "exclude_file_patterns": ["*.meta", "*.Designer.cs"] // 排除文件模式
  },
  "git": {
    "since": "2026-01-17",  // git log 起始日期
    "branch": ""             // 空 = 当前分支
  },
  "review": {
    "max_files_per_module": 30,  // 超过此数拆分为 Part1/Part2
    "parallel_agents": 18,       // 并行审查 agent 数
    "focus": {
      "logic_errors": [          // 必须覆盖的逻辑错误检查项
        "空引用风险",
        "数组/字典越界",
        "除零风险",
        "类型转换异常",
        "事件监听泄漏",
        "循环中修改集合",
        "条件判断逻辑错误",
        "多线程/重入安全",
        "资源未释放"
      ],
      "performance_issues": [    // 必须覆盖的性能检查项
        "LINQ/ToList() GC分配",
        "每帧重复创建临时对象",
        "字典双重查找",
        "字符串拼接在循环/高频调用中",
        "不必要的反射调用",
        "O(n²)嵌套循环"
      ]
    }
  },
  "output": {
    "language": "zh-CN",
    "generate_summary": true,
    "generate_full_report": true,
    "blame_authors": true,
    "author_alias": {             // 作者名归一化映射
      "GaoWenQiang": "gaowenqiang"
    }
  }
}
```

### focus 字段说明

`review.focus` 中列出的条目是**最低保证清单**，审查时必须覆盖。审查模型可以在此基础上自由扩展，发现清单之外的问题同样会被报告（如事件 ID 冲突、API 参数不匹配等）。

## 输出结构

每次审核生成带范围信息的独立目录，历次结果互不干扰：

```
AiDoc/CodeReview/
├── config.json
├── 2026-03-19_143000/
│   ├── module_commits.md        # 模块划分与变更文件清单
│   ├── review_ModuleA.md        # 各模块审查报告
│   ├── review_ModuleB.md
│   ├── review_*.md
│   ├── SUMMARY.md               # 汇总摘要 + 作者统计表
│   ├── FULL_REPORT.md           # 完整报告（含作者标注）
│   └── by_author/               # 按作者拆分
│       ├── INDEX.md             # 作者索引
│       ├── VERIFY.md            # 一致性校验（条目数 + 内容哈希）
│       ├── bugs_zhangsan.md     # 各作者的问题清单
│       └── bugs_未归属.md       # 无法追溯的问题
└── 202601261245@20260125@20260126/
    └── ...
```

## blame_split.py

阶段 5 的作者追溯由 `blame_split.py` 脚本完成，可独立运行：

```bash
python .agents/skills/code-review/blame_split.py \
  --root <repo-root> \
  --report <output-dir>/FULL_REPORT.md \
  --config AiDoc/CodeReview/config.json
```

### 功能

1. **解析** — 从 `FULL_REPORT.md` 提取所有 `### 问题N` 块，兼容多种字段格式
2. **文件索引** — `git ls-files` 构建索引，5 级路径匹配策略：
   - 精确匹配 → 大小写不敏感 → 磁盘检查 → 前缀补全 → 后缀/basename 兜底
3. **git blame** — 按行号精确定位作者，失败时 fallback 到 `git log`，每次 10s 超时
4. **作者归一化** — 按 `config.json` 的 `author_alias` 合并同名作者
5. **更新报告** — 在标题后追加 `【作者: xxx】`，末尾追加统计表
6. **按人拆分** — 每位作者生成独立 `.md`，附带 `VERIFY.md` 哈希校验

### 幂等性

脚本可重复运行，会自动清除上次的作者标注和统计表，不会重复叠加。

## 常用操作

**增量审核** — 修改 `config.json` 中的 `git.since` 日期，只审查新增变更：
```json
{ "git": { "since": "2026-03-01" } }
```

`target.directories` 就是这次审核/追溯的范围，填你的仓库里实际要检查的目录即可。

**只跑作者追溯** — 如果审查报告已存在，只需补跑阶段 5：
```bash
python .agents/skills/code-review/blame_split.py --root <repo-root> --report <FULL_REPORT路径> --config AiDoc/CodeReview/config.json
```

**对比趋势** — 不同日期的输出在各自目录下，可直接对比问题数量变化。

## 文件清单

```
.agents/skills/code-review/
├── SKILL.md           # 技能定义（流程、格式规范、踩坑提醒）
├── blame_split.py     # 作者追溯 + 按人拆分脚本
└── README.md          # 本文件
```
