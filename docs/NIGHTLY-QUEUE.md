# Where2Go 夜间任务队列(NIGHTLY-QUEUE)

> 夜班执行器(每晚 22:00 CST, qwen3.8-max)读取本文件,依次执行**未完成**条目。
> 队列空 → 整轮跳过(0 token)。白天把重活追加为条目;完成后更新该条状态。
>
> 条目格式:
> ```
> ## [TASK-xxx] 标题
> - 状态: pending | running | done | blocked | needs_review
> - 目标: <做什么>
> - 涉及: <文件/路径>
> - 验收: <可验证标准>
> - 结果: <夜班执行后回填>
> ```

---

## [TASK-1b] 四分类检索 + 归类去重模块(backend 数据层增强)

- 状态: running
- 目标: 实现需求四分类(自然风光 / 小城人文美食 / 滑雪场 / 运动)的 OSM 检索与归类,一个地物按优先级只归一类,消除 POC 中"自然/景点重复"。属 backend 数据源层,不依赖地图 UI/DB(可独立实现+单测)。
- 涉及: backend/data_sources/(overpass 归类)、backend/test_*.py
- 验收:
  1. 四分类各有 OSM tag 识别线索(见 docs/STAGE1-PLAN.md 表)
  2. 归类优先级:滑雪 > 运动 > 人文美食 > 自然;同实体跨 tag 只入一类
  3. 以去重键(osm id+type)去重
  4. 单测通过(pytest backend/)
- 结果: (待夜班回填)

---

## 追加模板(新任务复制此段)

## [TASK-xxx] 标题
- 状态: running
- 目标:
- 涉及:
- 验收:
- 结果: (待夜班回填)
