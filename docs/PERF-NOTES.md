# TASK-12 性能笔记(PERF-NOTES)

> 2026-10-07 夜班实测。工具:`tools/perf_probe.py`(插桩 amap 请求次数/耗时、intro 回填耗时、落库耗时、总墙钟)。
> 环境:容器直连高德(0.6s 进程级节流)+ token-plan qwen3.8-max;城市=成都(全新冷抓,库内无该城市行)。

## 基线(优化前,同步 intro)

| 阶段 | 总墙钟 | amap 网络 | LLM intro | 落库+其他 |
|---|---|---|---|---|
| 0_50 冷抓(满配额 540) | **173.4s** | 20 calls / 12.2s | 40 条 / **161.0s** | geocode 0.1s + upsert 0.1s |
| 50_100 ring 冷抓(4格×6组) | **198.2s** | 88 calls / 52.4s | 40 条 / **145.7s** | ~0s |
| 0_50 扩抓 round2 | **168.1s** | 5 calls / 2.5s | 40 条 / **165.5s** | ~0s |

结论(按队列给的三条线索核对):

1. **线索①成立且是绝对大头**:LLM intro 同步回填占墙钟 **73%~98%**(单条 LLM 3~5s,40 条 / 4 workers ≈ 150~165s)。amap 检索本身只有 2.5~52s。
2. 线索②(amap 串行×节流)真实存在但不是主要矛盾:0_50 满配额 20 calls 才 12.2s(节流 0.6s×20=12s,几乎全是节流),ring 88 calls 52.4s(节流 52.8s)——**耗时≈纯节流时间,RTT 已被并行度=0 摊在节流窗内**。组间并行的收益上限只有 RTT 重叠部分,且要冒 QPS 10021 风险 → **本轮不做**(优化对象占比 <30%,先动大头)。
3. 线索③(空页请求)未证实:`_paged` 已有「配额满即停/页不满即停」,实测无明显空页浪费 → 不做。

## 优化实施(候选 a:intro 回填改异步后台)

照 stays(TASK-6c)的 estimating 口径:

- `services/intro.py` 新增后台回填:`start_backfill()`(提交到模块级 ThreadPoolExecutor,任务自开 session(用 `session.get_bind()` 拿 engine,测试 tmp 库天然隔离)、自 commit、异常吞掉降级不抛;`(city,band)` inflight 去重防重复提交;`INLINE_EXECUTOR` + `set_backfill_executor()` 供测试/CLI 注入变同步)。
- `services/place_loader.load_segment`:抓取入库 commit 后,缺简介行 >0 → 提交后台回填并**立即返回**(intro=null);新增 `sync_intros=True` 参数给 CLI 保留同步口径(`--sync-intros`)。
- `intro_stats` 口径:异步提交时返回 `None` → 前端 `introNote()` 走既有分支「待补简介 N 条(点『补简介』)」——**前端零改动、/api/places 响应键零变化**;INLINE(测试)模式下同步执行并返回真实 stats,既有断言零改动(conftest autouse 注入)。
- 兜底不变:前端「补简介」按钮(/api/places/intros)仍可手动同步补;读库路径本来就零 LLM。
- INTRO_BATCH_LIMIT 维持 40(后台跑不占首屏,无降到 15 的必要);候选 b/c/d 均按上表数据不做。

## 优化后(实测,2026-10-07 同晚)

| 阶段 | 基线墙钟 | 优化后墙钟 | 降幅 |
|---|---|---|---|
| 0_50 冷抓(满配额,武汉 vs 成都) | 173.4s | **12.3s** | **-93%** |
| 50_100 ring 冷抓(武汉 vs 成都) | 198.2s | **50.2s** | **-75%**(剩余全是 amap 节流 84 calls×0.6s) |
| 0_50 扩抓 round2(武汉 vs 成都) | 168.1s | **3.0s** | **-98%** |

真机冒烟(uvicorn :8000 已重启,西安 0_50 全新冷抓):`GET /api/places?page_size=15` **3.3s** 返回 15/29 条(source=amap、intro_stats=None、intro_pending=29,响应键零变化);~2.5min 后复查库内 intro 缺 29→**1**(后台线程池自动补齐,前端「补简介」按钮仍可兜底);二次查询 source=db 秒回。

测试:`backend/test_intro_backfill.py` 9 用例(真线程池路径:返回不阻塞/sync_intros 旧契约/InlineExecutor 返回 stats/no-intros 零 LLM/inflight 去重/worker 吞异常/worker 自开 session/读库路径零回填);conftest autouse 注入 InlineExecutor → 既有 intro_stats 断言零改动。pytest backend/ = **1352 passed**(1343 基线零回归)。

坑记录:SQLite 读事务(SHARED 锁)会挡后台 worker 写库 → `start_backfill` 提交前 `session.rollback()` 释放快照;真线程池测试断言前也要先 rollback 再读。

候选 b(INTRO_BATCH_LIMIT 降 15)/c(组间并行)/d(空页)均未做,理由见基线结论 2/3:intro 移后台后剩余墙钟几乎 = amap 节流时间,再优化空间有限且 c 有 QPS 10021 风险。
